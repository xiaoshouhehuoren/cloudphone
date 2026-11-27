package com.g98.sensors;

import android.hardware.sensors.*;
import android.hardware.common.*;
import android.hardware.common.fmq.*;
import android.os.*;
import java.io.*;
import java.lang.reflect.*;
import java.nio.*;
import java.nio.channels.*;
import java.util.Random;

public class SensorHalJava {
    static void log(String s) {
        try (FileOutputStream f = new FileOutputStream("/data/local/tmp/sensors_java.log", true)) {
            f.write((s + "\n").getBytes("UTF-8"));
        } catch (Throwable t) {}
    }


    static class Trace {
        static final int N = 10;
        static final int[] TYPES = {1, 4, 2, 11, 9, 10, 5, 8, 6, 19};
        static final int MAGIC = 0x47393853;
        final int[] dims = new int[N];
        final int[] counts = new int[N];
        final long[][] ts = new long[N][];
        final float[][] vals = new float[N][];
        final int[] cursor = new int[N];
        final long[] lastPt = new long[N];
        long durationNs;
        long startNs;
        long stepBase;

        static int slotFor(int type) {
            for (int i = 0; i < N; i++) if (TYPES[i] == type) return i;
            return -1;
        }

        static Trace load(String path) {
            Trace t = new Trace();
            try (DataInputStream in = new DataInputStream(
                    new BufferedInputStream(new FileInputStream(path), 1 << 20))) {
                int magic = in.readInt();
                int ver = in.readInt();
                int count = in.readInt();
                t.durationNs = in.readLong();
                if (magic != MAGIC || ver != 1 || count <= 0 || t.durationNs <= 0) {
                    log("trace bad header magic=" + Integer.toHexString(magic) + " ver=" + ver
                        + " count=" + count + " dur=" + t.durationNs);
                    return null;
                }
                for (int k = 0; k < count; k++) {
                    int type = in.readInt();
                    int dim = in.readInt();
                    int n = in.readInt();
                    if (dim <= 0 || dim > 16 || n <= 0) {
                        log("trace bad block type=" + type + " dim=" + dim + " n=" + n);
                        return null;
                    }
                    long[] tts = new long[n];
                    float[] vv = new float[n * dim];
                    for (int i = 0; i < n; i++) {
                        tts[i] = in.readLong();
                        for (int j = 0; j < dim; j++) vv[i * dim + j] = in.readFloat();
                    }
                    int slot = slotFor(type);
                    if (slot < 0) {
                        log("trace skip type=" + type);
                        continue;
                    }
                    t.ts[slot] = tts;
                    t.vals[slot] = vv;
                    t.dims[slot] = dim;
                    t.counts[slot] = n;
                }
                int ready = 0;
                for (int i = 0; i < N; i++) if (t.counts[i] > 0) ready++;
                log("trace loaded " + path + " ready=" + ready + " dur=" + t.durationNs);
                return ready == N ? t : null;
            } catch (Throwable e) {
                log("trace load error: " + e);
                return null;
            }
        }

        // Returns the offset (sample * dim) for this sensor at wall-clock ts, or -1.
        int offset(int slot, long now) {
            int n = counts[slot];
            if (n <= 0 || durationNs <= 0) return -1;
            long pt = (now - startNs) % durationNs;
            if (pt < 0) pt += durationNs;
            if (pt < lastPt[slot]) {
                if (TYPES[slot] == 19) stepBase += (long) vals[slot][(n - 1) * dims[slot]];
                cursor[slot] = 0;
            }
            lastPt[slot] = pt;
            int c = cursor[slot];
            if (c >= n) c = n - 1;
            while (c + 1 < n && ts[slot][c + 1] <= pt) c++;
            cursor[slot] = c;
            return c * dims[slot];
        }

        float value(int slot, int off, int i) {
            float[] v = vals[slot];
            int o = off + i;
            return (o >= 0 && o < v.length) ? v[o] : 0.0f;
        }
    }

    // 手机实时传感器：手机浏览器把选中的传感器通过 dashboard 写进
    // /data/local/tmp/g98-sensors-live.bin，HAL 在这里读。固定 180 字节布局：
    // magic(int) ver(int) mask(int) wallMs(long) + 10 个槽位各 4 个 float。
    // mask 某位为 1 表示该槽位用手机实时值，为 0 的槽位继续回放真机 trace。
    static class Live {
        static final int MAGIC = 0x4739384C;  // "G98L"
        static final int SIZE = 12 + 8 + 10 * 16;
        final int mask;
        final long wallMs;
        final float[][] slots = new float[10][4];
        final long readAt = System.currentTimeMillis();

        Live(int mask, long wallMs) { this.mask = mask; this.wallMs = wallMs; }

        boolean fresh() { return System.currentTimeMillis() - wallMs < 1500; }

        float[] get(int idx) {
            return ((mask >> idx) & 1) != 0 ? slots[idx] : null;
        }

        static Live read(String path) {
            byte[] buf = new byte[SIZE];
            try (FileInputStream in = new FileInputStream(path)) {
                int n = 0, r;
                while (n < SIZE && (r = in.read(buf, n, SIZE - n)) > 0) n += r;
                if (n < SIZE) return null;
            } catch (Throwable t) {
                return null;
            }
            ByteBuffer b = ByteBuffer.wrap(buf).order(ByteOrder.LITTLE_ENDIAN);
            if (b.getInt(0) != MAGIC || b.getInt(4) != 1) return null;
            Live l = new Live(b.getInt(8), b.getLong(12));
            int off = 20;
            for (int i = 0; i < 10; i++) {
                for (int k = 0; k < 4; k++) l.slots[i][k] = b.getFloat(off + i * 16 + k * 4);
            }
            return l;
        }
    }

    public static class Hal extends ISensors.Stub {
        private MappedByteBuffer queue;
        private RandomAccessFile queueStream;
        private int quantum;
        private int readOff;
        private int writeOff;
        private int dataOff;
        private int flagOff;
        private int capacity;
        private long queueAddr;
        private java.lang.Process futexProc;
        private volatile boolean active;
        private long lastLogSeq = -1;
        private Thread writer;
        private Trace trace;
        // 每台云手机回放同一份真机 trace，但相位与偏置按实例名做确定性随机，
        // 这样五台的数据互不相同（加速度计曲线、罗盘朝向、光照水平都不一样）。
        private static final String SEED_PATH = "/data/local/tmp/g98-sensors-seed.txt";
        private long devSeed;
        private long devShiftNs;
        private final float[][] devBias = new float[N][4];
        private float devLightScale = 1.0f;
        private long eventSeq;
        private static final String LIVE_PATH = "/data/local/tmp/g98-sensors-live.bin";
        private volatile Live live;
        private long liveReadAt;
        private long liveEvents;
        private int sampleSeq;
        private final boolean[] liveLogged = new boolean[10];

        private static final int N = 10;
        private static final String[] TYPES_SHORT = {"acc", "gyro", "mag", "rv", "grav", "lin", "light", "prox", "pressure", "step"};
        private final int[] handles = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10};
        private final int[] types = {1, 4, 2, 11, 9, 10, 5, 8, 6, 19};
        private final String[] names = {
            "Fake Accelerometer", "Fake Gyroscope", "Fake Magnetometer", "Fake Rotation Vector",
            "Fake Gravity", "Fake Linear Acceleration", "Fake Light", "Fake Proximity",
            "Fake Pressure", "Fake Step Counter"
        };
        // OnePlus 9 / sensor-lab catalog values (icm4x6xx, mmc56x3x, tcs3701, stk33502)
        private final float[] maxRanges = {156.906f, 34.907f, 3000.029f, 1.0f, 156.910f, 156.910f, 1.0f, 5.0f, 1100.0f, 4294967296.0f};
        private final float[] resolutions = {0.004788f, 0.001065f, 0.0976f, 0.010000f, 0.010000f, 0.010000f, 0.010000f, 1.0f, 0.01f, 1.0f};
        private final float[] powers = {0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.5f, 0.1f, 0.1f, 0.1f, 0.1f};
        private final int[] minDelays = {5000, 5000, 10000, 5000, 5000, 5000, 0, 0, 0, 0};
        private final int[] flags = {0, 0, 0, 0, 0, 0, 2, 2, 0, 2};
        private final boolean[] enabled = new boolean[N + 1];
        private final long[] periodNs = new long[N + 1];
        private final long[] nextNs = new long[N + 1];
        private long stepCount = 0;
        private long nextStepNs = 0;

        private static boolean futexTried;
        private static Method futexMethod;
        private static boolean futexStatic;
        private static Object futexTarget;

        private static long bufferAddress(ByteBuffer b) {
            try {
                Field f = Class.forName("java.nio.Buffer").getDeclaredField("address");
                f.setAccessible(true);
                return f.getLong(b);
            } catch (Throwable t) {
                log("bufferAddress error: " + t);
                return 0;
            }
        }

        private static void initFutex() {
            if (futexTried) return;
            futexTried = true;
            try {
                Class<?> os = Class.forName("android.system.Os");
                futexMethod = os.getDeclaredMethod("futex", long.class, int.class, int.class, long.class, int.class);
                futexMethod.setAccessible(true);
                futexStatic = true;
                log("futex via android.system.Os");
                return;
            } catch (Throwable t) {
                log("Os.futex absent: " + t);
            }
            try {
                Class<?> lc = Class.forName("libcore.io.Linux");
                futexMethod = lc.getDeclaredMethod("futex", long.class, int.class, int.class, long.class, int.class);
                futexMethod.setAccessible(true);
                futexStatic = false;
                Class<?> lib = Class.forName("libcore.io.Libcore");
                Field f = lib.getField("os");
                futexTarget = f.get(null);
                log("futex via libcore.io.Linux");
                return;
            } catch (Throwable t) {
                log("Linux.futex absent: " + t);
            }
        }

        private void wakeFutex() {
            if (queueAddr == 0) return;
            try {
                initFutex();
                if (futexMethod == null) return;
                long addr = queueAddr + flagOff;
                try {
                    futexMethod.invoke(futexStatic ? null : futexTarget, addr, 129, 1, 0L, 0);
                } catch (Throwable t1) {
                    futexMethod.invoke(futexStatic ? null : futexTarget, addr, 1, 1, 0L, 0);
                }
            } catch (Throwable t) {
                log("wakeFutex error: " + t);
            }
        }

        private void startFutexHelper(int fd, int off) {
            try {
                if (futexProc != null) futexProc.destroy();
                try {
                    new ProcessBuilder("/system/bin/sh", "-c", "pkill -9 -x futexwake").start().waitFor();
                } catch (Throwable ignored) {}
                ProcessBuilder pb = new ProcessBuilder("/data/local/tmp/futexwake",
                        String.valueOf(android.os.Process.myPid()), String.valueOf(fd), String.valueOf(off));
                pb.redirectErrorStream(true);
                pb.redirectOutput(new File("/data/local/tmp/futexwake.log"));
                futexProc = pb.start();
                log("futex helper started");
            } catch (Throwable t) {
                log("futex helper error: " + t);
            }
        }

        private void mapQueue(MQDescriptor<Event, Byte> desc) {
            try {
                if (desc == null || desc.handle == null || desc.handle.fds.length == 0) {
                    log("mapQueue no fd");
                    return;
                }
                ParcelFileDescriptor pfd = desc.handle.fds[0];
                quantum = desc.quantum;
                readOff = (int) desc.grantors[0].offset;
                writeOff = (int) desc.grantors[1].offset;
                dataOff = (int) desc.grantors[2].offset;
                flagOff = (int) desc.grantors[3].offset;
                int dataLen = (int) desc.grantors[2].extent;
                capacity = dataLen / quantum;
                int mapLen = flagOff + 4;
                queueStream = new RandomAccessFile("/proc/self/fd/" + pfd.getFd(), "rw");
                queue = queueStream.getChannel().map(FileChannel.MapMode.READ_WRITE, 0, mapLen);
                queue.order(ByteOrder.LITTLE_ENDIAN);
                queueAddr = bufferAddress(queue);
                startFutexHelper(pfd.getFd(), flagOff);
                log("mapQueue fd=" + pfd.getFd() + " quantum=" + quantum + " cap=" + capacity
                    + " readOff=" + readOff + " writeOff=" + writeOff + " dataOff=" + dataOff + " flagOff=" + flagOff
                    + " addr=" + queueAddr);
            } catch (Throwable t) {
                log("mapQueue error: " + t);
            }
        }

        private int sensorIndex(int handle) {
            for (int i = 0; i < N; i++) if (handles[i] == handle) return i;
            return -1;
        }

        private void startWriter() {
            if (writer != null && writer.isAlive()) return;
            if (trace != null && trace.startNs == 0) trace.startNs = SystemClock.elapsedRealtimeNanos();
            writer = new Thread(new Runnable() {
                @Override public void run() {
                    log("writer start");
                    long seq = 0;
                    while (true) {
                        try {
                            boolean any = false;
                            long now = SystemClock.elapsedRealtimeNanos();
                            refreshLive(now);
                            for (int i = 0; i < N; i++) {
                                if (!enabled[handles[i]]) continue;
                                any = true;
                                long p = periodNs[handles[i]];
                                if (p <= 0) p = 200000000L;
                                if (now < nextNs[handles[i]]) continue;
                                nextNs[handles[i]] = now + p;
                                if (writeSensor(i, now)) seq++; else Thread.sleep(2);
                            }
                            if (!any) Thread.sleep(50);
                            else if ((seq % 200) == 0 && seq != lastLogSeq) {
                                lastLogSeq = seq;
                                log("writer seq=" + seq + " wp=" + queue.getLong(writeOff) + " rp=" + queue.getLong(readOff));
                            }
                            if (any) Thread.sleep(5);
                        } catch (Throwable t) {
                            log("writer error: " + t);
                            try { Thread.sleep(200); } catch (Throwable t2) {}
                        }
                    }
                }
            }, "g98-sensor-writer");
            writer.setDaemon(true);
            writer.start();
        }

        private void refreshLive(long now) {
            if (now - liveReadAt < 20000000L) return;   // 最多 50Hz 读一次文件
            liveReadAt = now;
            Live l = Live.read(LIVE_PATH);
            if (l != null && !l.fresh()) l = null;
            if (l != null && (live == null || l.mask != live.mask)) {
                StringBuilder sb = new StringBuilder("live mask=0x").append(Integer.toHexString(l.mask));
                for (int i = 0; i < N; i++) {
                    float[] v = l.get(i);
                    if (v == null) continue;
                    sb.append(" ").append(TYPES_SHORT[i]).append("=").append(v[0]).append(",").append(v[1]).append(",").append(v[2]);
                }
                log(sb.toString());
            }
            live = l;
        }

        private float[] liveValue(int idx) {
            Live l = live;
            if (l == null) return null;
            return l.get(idx);
        }

        private float pick(float[] lv, int k, int idx, int off) {
            if (lv != null && k < lv.length) return lv[k];
            float v = off >= 0 ? trace.value(idx, off, k) : 0.0f;
            if (off >= 0) {
                v += devBias[idx][k & 3];
                if (idx == 6) v *= devLightScale;                        // 光照：整体系数
                if ((idx == 6 || idx == 7 || idx == 8) && v < 0) v = 0;  // 非负量
            }
            return v;
        }

        private static long fnv(String s) {
            long h = 0xcbf29ce484222325L;
            for (int i = 0; i < s.length(); i++) {
                h ^= s.charAt(i);
                h *= 0x100000001b3L;
            }
            return h;
        }

        // 以实例名（/data/local/tmp/g98-sensors-seed.txt）为种子：trace 起始相位 +
        // 每个传感器的固定偏置/光照系数都确定性地随机，重启后保持同一台设备的“人格”。
        private void initDeviceVariation() {
            String id = "";
            try (DataInputStream in = new DataInputStream(new FileInputStream(SEED_PATH))) {
                byte[] b = new byte[64];
                int n = in.read(b);
                if (n > 0) id = new String(b, 0, n, "UTF-8").trim();
            } catch (Throwable t) {}
            if (id.isEmpty()) {
                try { id = Build.getSerial(); } catch (Throwable t) { id = null; }
                if (id == null) id = "";
            }
            if (id.isEmpty()) id = "g98";
            devSeed = fnv(id);
            Random r = new Random(devSeed);
            if (trace != null && trace.durationNs > 0) devShiftNs = (long) (r.nextDouble() * trace.durationNs);
            devBias[0][0] = (float) ((r.nextDouble() - 0.5) * 0.7);   // accel ±0.35 m/s^2
            devBias[0][1] = (float) ((r.nextDouble() - 0.5) * 0.7);
            devBias[0][2] = (float) ((r.nextDouble() - 0.5) * 0.7);
            devBias[1][0] = (float) ((r.nextDouble() - 0.5) * 0.06);  // gyro ±0.03 rad/s
            devBias[1][1] = (float) ((r.nextDouble() - 0.5) * 0.06);
            devBias[1][2] = (float) ((r.nextDouble() - 0.5) * 0.06);
            devBias[2][0] = (float) ((r.nextDouble() - 0.5) * 8.0);   // mag ±4 uT
            devBias[2][1] = (float) ((r.nextDouble() - 0.5) * 8.0);
            devBias[2][2] = (float) ((r.nextDouble() - 0.5) * 8.0);
            devBias[4][0] = (float) ((r.nextDouble() - 0.5) * 0.4);   // gravity ±0.2
            devBias[4][1] = (float) ((r.nextDouble() - 0.5) * 0.4);
            devBias[4][2] = (float) ((r.nextDouble() - 0.5) * 0.4);
            devBias[5][0] = (float) ((r.nextDouble() - 0.5) * 0.4);   // linear acc ±0.2
            devBias[5][1] = (float) ((r.nextDouble() - 0.5) * 0.4);
            devBias[5][2] = (float) ((r.nextDouble() - 0.5) * 0.4);
            devBias[8][0] = (float) ((r.nextDouble() - 0.5) * 12.0);  // pressure ±6 hPa
            devLightScale = (float) (0.55 + r.nextDouble() * 0.95);   // light 0.55x .. 1.5x
            log("device variation id=" + id + " seed=" + Long.toHexString(devSeed)
                + " shiftMs=" + (devShiftNs / 1000000L) + " lightScale=" + devLightScale
                + " accBias=" + devBias[0][0] + "," + devBias[0][1] + "," + devBias[0][2]);
        }

        private void putFloat(int base, int off, float v) {
            queue.putFloat(base + off, v);
        }

        private boolean writeSensor(int idx, long ts) {
            if (queue == null) return false;
            int handle = handles[idx];
            int type = types[idx];
            long wp = queue.getLong(writeOff);
            long rp = queue.getLong(readOff);
            if (wp - rp >= capacity - 1) return false;   // reader behind: drop instead of overwriting unread slots
            int base = dataOff + (int)(wp % capacity) * quantum;
            for (int i = 0; i < quantum; i++) queue.put(base + i, (byte) 0);
            queue.putLong(base, ts);
            queue.putInt(base + 8, handle);
            queue.putInt(base + 12, type);
            int off = (trace == null) ? -1 : trace.offset(idx, ts + devShiftNs);
            if (trace != null && off >= 0 && eventSeq < 20) {
                StringBuilder sb = new StringBuilder("evt type=").append(type);
                int dim = Math.min(3, trace.dims[idx]);
                for (int k = 0; k < dim; k++) sb.append(" ").append(trace.value(idx, off, k));
                log(sb.toString());
                eventSeq++;
            }
            float[] lv = liveValue(idx);
            if (lv != null && (!liveLogged[idx] || (++liveEvents % 500) == 0)) {
                if (!liveLogged[idx]) liveLogged[idx] = true;
                log("live type=" + type + " v=" + lv[0] + "," + lv[1] + "," + lv[2]);
            }
            if (off >= 0 || lv != null) {
                if (type == 5 || type == 6 || type == 8) {
                    queue.put(base + 16, (byte) 4);
                    putFloat(base, 24, pick(lv, 0, idx, off));
                } else if (type == 19) {
                    queue.put(base + 16, (byte) 5);
                    queue.putLong(base + 24, trace.stepBase + (long) trace.value(idx, off, 0));
                } else if (type == 11) {
                    queue.put(base + 16, (byte) 1);
                    putFloat(base, 24, pick(lv, 0, idx, off));
                    putFloat(base, 28, pick(lv, 1, idx, off));
                    putFloat(base, 32, pick(lv, 2, idx, off));
                    putFloat(base, 36, pick(lv, 3, idx, off));
                } else {
                    queue.put(base + 16, (byte) 0);
                    putFloat(base, 24, pick(lv, 0, idx, off));
                    putFloat(base, 28, pick(lv, 1, idx, off));
                    putFloat(base, 32, pick(lv, 2, idx, off));
                    // 周期性打出"补偿后"的加速度采样：五台各自 seed 不同，
                    // 这些值必须逐台不同，用于验证随机化真的落到事件上。
                    if (type == 1 && (sampleSeq++ % 150) == 0) {
                        log("sample acc=" + pick(lv, 0, idx, off) + "," + pick(lv, 1, idx, off)
                                + "," + pick(lv, 2, idx, off) + " n=" + sampleSeq);
                    }
                }
            } else {
                double t = ts / 1000000000.0;
                if (type == 5 || type == 6 || type == 8) {
                    queue.put(base + 16, (byte) 4);
                    float v = type == 5 ? 300.0f : (type == 6 ? 1013.25f : 5.0f);
                    putFloat(base, 24, v);
                } else if (type == 19) {
                    if (ts > nextStepNs) {
                        stepCount++;
                        nextStepNs = ts + 1000000000L;
                    }
                    queue.put(base + 16, (byte) 5);
                    queue.putLong(base + 24, stepCount);
                } else if (type == 11) {
                    queue.put(base + 16, (byte) 1);
                    putFloat(base, 24, 0.0f);
                    putFloat(base, 28, 0.0f);
                    putFloat(base, 32, 0.0f);
                    putFloat(base, 36, 1.0f);
                } else {
                    queue.put(base + 16, (byte) 0);
                    if (type == 1) {
                        putFloat(base, 24, 0.0f); putFloat(base, 28, 0.0f); putFloat(base, 32, 9.80665f);
                    } else if (type == 4) {
                        putFloat(base, 24, (float)(0.02 * Math.sin(t)));
                        putFloat(base, 28, (float)(0.02 * Math.cos(t)));
                        putFloat(base, 32, 0.005f);
                    } else if (type == 2) {
                        putFloat(base, 24, 22.0f); putFloat(base, 28, 0.0f); putFloat(base, 32, -42.0f);
                    } else if (type == 9) {
                        putFloat(base, 24, 0.0f); putFloat(base, 28, 0.0f); putFloat(base, 32, 9.80665f);
                    } else {
                        putFloat(base, 24, 0.0f); putFloat(base, 28, 0.0f); putFloat(base, 32, 0.0f);
                    }
                }
            }
            // ARM: make the payload visible before SensorService sees the new write index.
            java.lang.invoke.VarHandle.fullFence();
            queue.putLong(writeOff, wp + 1);
            queue.putInt(flagOff, 1);
            wakeFutex();
            return true;
        }

        @Override public int getInterfaceVersion() { return 1; }

        @Override public SensorInfo[] getSensorsList() {
            SensorInfo[] out = new SensorInfo[N];
            String[] typeNames = {
                "android.sensor.accelerometer", "android.sensor.gyroscope", "android.sensor.magnetic_field",
                "android.sensor.rotation_vector", "android.sensor.gravity", "android.sensor.linear_acceleration",
                "android.sensor.light", "android.sensor.proximity", "android.sensor.pressure", "android.sensor.step_counter"
            };
            for (int i = 0; i < N; i++) {
                SensorInfo s = new SensorInfo();
                s.sensorHandle = handles[i];
                s.name = names[i];
                s.vendor = "g98";
                s.version = 1;
                s.type = types[i];
                s.typeAsString = typeNames[i];
                s.maxRange = maxRanges[i];
                s.resolution = resolutions[i];
                s.power = powers[i];
                s.minDelayUs = minDelays[i];
                s.fifoReservedEventCount = 0;
                s.fifoMaxEventCount = 0;
                s.requiredPermission = "";
                s.maxDelayUs = 0;
                s.flags = flags[i];
                out[i] = s;
            }
            log("getSensorsList -> " + N);
            return out;
        }

        @Override public void initialize(MQDescriptor<Event, Byte> eventQueueDescriptor,
                                         MQDescriptor<Integer, Byte> wakeLockDescriptor,
                                         ISensorsCallback sensorsCallback) {
            log("initialize called");
            mapQueue(eventQueueDescriptor);
            trace = Trace.load("/data/local/tmp/g98_sensors_trace.bin");
            initDeviceVariation();
            try {
                log("event quantum=" + eventQueueDescriptor.quantum + " flags=" + eventQueueDescriptor.flags
                    + " grantors=" + eventQueueDescriptor.grantors.length);
                for (int i = 0; i < eventQueueDescriptor.grantors.length; i++) {
                    GrantorDescriptor g = eventQueueDescriptor.grantors[i];
                    log("event grantor[" + i + "] fdIndex=" + g.fdIndex + " offset=" + g.offset + " extent=" + g.extent);
                }
                log("wake quantum=" + wakeLockDescriptor.quantum + " flags=" + wakeLockDescriptor.flags);
            } catch (Throwable t) {
                log("initialize log error: " + t);
            }
        }

        @Override public void activate(int sensorHandle, boolean enable) {
            log("activate handle=" + sensorHandle + " enabled=" + enable);
            int i = sensorIndex(sensorHandle);
            if (i >= 0) {
                enabled[sensorHandle] = enable;
                nextNs[sensorHandle] = SystemClock.elapsedRealtimeNanos();
            }
            active = false;
            for (int j = 0; j < N; j++) if (enabled[handles[j]]) active = true;
            if (active) startWriter();
        }
        @Override public void batch(int sensorHandle, long samplingPeriodNs, long maxReportLatencyNs) {
            log("batch handle=" + sensorHandle + " period=" + samplingPeriodNs + " latency=" + maxReportLatencyNs);
            if (samplingPeriodNs > 0) periodNs[sensorHandle] = samplingPeriodNs;
        }
        @Override public void flush(int sensorHandle) {
            log("flush handle=" + sensorHandle);
            try {
                if (queue == null) return;
                long wp = queue.getLong(writeOff);
                int base = dataOff + (int)(wp % capacity) * quantum;
                for (int i = 0; i < quantum; i++) queue.put(base + i, (byte) 0);
                queue.putLong(base, SystemClock.elapsedRealtimeNanos());
                queue.putInt(base + 8, sensorHandle);
                queue.putInt(base + 12, 0);
                queue.put(base + 16, (byte) 3);
                queue.putInt(base + 24, 1);
                queue.putLong(writeOff, wp + 1);
                queue.putInt(flagOff, 1);
                wakeFutex();
            } catch (Throwable t) { log("flush error: " + t); }
        }
        @Override public void injectSensorData(Event event) {
            log("inject handle=" + event.sensorHandle + " type=" + event.sensorType);
        }
        @Override public void setOperationMode(int mode) { log("setOperationMode=" + mode); }
        @Override public int configDirectReport(int sensorHandle, int channelHandle, int rate) {
            log("configDirectReport handle=" + sensorHandle + " channel=" + channelHandle + " rate=" + rate);
            return 0;
        }
        @Override public int registerDirectChannel(ISensors.SharedMemInfo mem) { log("registerDirectChannel"); return -1; }
        @Override public void unregisterDirectChannel(int channelHandle) { log("unregisterDirectChannel=" + channelHandle); }
    }

    static final String NAME = "android.hardware.sensors.ISensors/default";

    public static void main(String[] args) throws Exception {
        log("main start");
        Class<?> sm = Class.forName("android.os.ServiceManager");
        Method add = sm.getMethod("addService", String.class, IBinder.class);
        Method get = sm.getMethod("getService", String.class);
        Method check = null;
        try { check = sm.getMethod("checkService", String.class); } catch (Throwable ignored) {}
        Object hal = new Hal();
        // Add exactly once. On this platform addService(String, IBinder) is void, so the
        // return value says nothing; calling it 120 times just re-registers the name and
        // made the log look like a failure while the service was already live.
        add.invoke(null, NAME, hal);
        boolean ok = false;
        for (int i = 0; i < 60; i++) {
            Object svc = null;
            try { svc = get.invoke(null, NAME); } catch (Throwable ignored) {}
            if (svc == null && check != null) {
                try { svc = check.invoke(null, NAME); } catch (Throwable ignored) {}
            }
            if (svc != null) {
                ok = true;
                log("addService ok (getService returned " + (svc == hal ? "our binder" : svc.getClass().getName()) + ")");
                break;
            }
            Thread.sleep(250);
        }
        if (!ok) log("addService FAILED: getService still null");
        while (true) Thread.sleep(1000);
    }
}
