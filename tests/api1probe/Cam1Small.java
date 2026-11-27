package com.g98.probe;

import android.hardware.Camera;
import android.graphics.SurfaceTexture;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.PrintWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;

public class Cam1Small {
    static int PW = 640, PH = 480;
    static PrintWriter out;
    static void log(String s) {
        try {
            out.println(new SimpleDateFormat("HH:mm:ss.SSS").format(new Date()) + " " + s);
            out.flush();
        } catch (Throwable t) {}
        System.out.println(s);
    }
    public static void main(String[] args) {
        try {
            out = new PrintWriter(new FileWriter("/data/local/tmp/cam1small.log", true), true);
        } catch (Throwable t) {
            System.out.println("log open failed " + t);
        }
        log("=== cam1 small-size probe ===");
        try {
            if (android.os.Looper.getMainLooper() == null) {
                android.os.Looper.prepareMainLooper();
            }
            Class<?> at = Class.forName("android.app.ActivityThread");
            Object thread = at.getMethod("systemMain").invoke(null);
            android.content.Context sys =
                    (android.content.Context) at.getMethod("getSystemContext").invoke(thread);
            android.app.Application app = new android.app.Application();
            java.lang.reflect.Method attach = android.app.Application.class
                    .getDeclaredMethod("attach", android.content.Context.class);
            attach.setAccessible(true);
            attach.invoke(app, sys);
            java.lang.reflect.Field f = at.getDeclaredField("mInitialApplication");
            f.setAccessible(true);
            f.set(thread, app);
            log("context ready: " + at.getMethod("currentApplication").invoke(null));
        } catch (Throwable t) {
            log("context setup failed: " + t);
            StackTraceElement[] st = t.getStackTrace();
            for (int i = 0; i < Math.min(st.length, 6); i++) log("   at " + st[i]);
        }
        int n = 0;
        try { n = Camera.getNumberOfCameras(); } catch (Throwable t) { log("count failed " + t); }
        log("cameras=" + n);
        if (args.length > 0) {
            if (args.length > 2) { PW = Integer.parseInt(args[1]); PH = Integer.parseInt(args[2]); }
            runOne(Integer.parseInt(args[0]));
        } else {
            for (int id = 0; id < n; id++) {
                runOne(id);
            }
        }
        log("=== probe end ===");
        try { out.close(); } catch (Throwable t) {}
        System.exit(0);
    }

    static void runOne(final int id) {
        final Camera c;
        try {
            c = Camera.open(id);
        } catch (Throwable t) {
            log("camera " + id + " open failed: " + t);
            return;
        }
        log("camera " + id + " opened");
        try {
            Camera.Parameters p = c.getParameters();
            List<Camera.Size> pv = p.getSupportedPreviewSizes();
            StringBuilder sb = new StringBuilder();
            if (pv != null) for (Camera.Size s : pv) sb.append(s.width).append("x").append(s.height).append(" ");
            log("camera " + id + " supportedPreview=[" + sb.toString().trim() + "]");
            log("camera " + id + " defaultPreview=" + p.getPreviewSize().width + "x" + p.getPreviewSize().height);
            p.setPreviewSize(PW, PH);
            c.setParameters(p);
            log("camera " + id + " setPreviewSize " + PW + "x" + PH + " ok, now=" +
                    c.getParameters().getPreviewSize().width + "x" + c.getParameters().getPreviewSize().height);
            final int[] frames = {0};
            final int[] bytes = {0};
            final boolean[] saved = {false};
            c.setPreviewCallback(new Camera.PreviewCallback() {
                public void onPreviewFrame(byte[] data, Camera camera) {
                    frames[0]++;
                    if (data != null) bytes[0] = data.length;
                    if (!saved[0] && data != null) {
                        saved[0] = true;
                        try {
                            FileOutputStream fo = new FileOutputStream(
                                    "/data/local/tmp/cam1_" + id + "_" + PW + "x" + PH + ".yuv");
                            fo.write(data);
                            fo.close();
                        } catch (Throwable t) { log("save failed " + t); }
                    }
                }
            });
            SurfaceTexture st = new SurfaceTexture(0);
            c.setPreviewTexture(st);
            c.startPreview();
            log("camera " + id + " startPreview ok");
            new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(new Runnable() {
                public void run() {
                    log("camera " + id + " RESULT frames=" + frames[0] + " bytes=" + bytes[0] +
                            " expected=" + (PW * PH * 3 / 2) + " saved=" + saved[0]);
                    try { c.stopPreview(); } catch (Throwable t) {}
                    try { c.release(); log("camera " + id + " released"); } catch (Throwable t) {}
                    System.exit(0);
                }
            }, 10000);
            android.os.Looper.loop();
            return;
        } catch (Throwable t) {
            log("camera " + id + " FAILED: " + t);
            StackTraceElement[] st = t.getStackTrace();
            for (int i = 0; i < Math.min(st.length, 6); i++) log("   at " + st[i]);
        } finally {
            try { c.release(); log("camera " + id + " released"); } catch (Throwable t) {}
        }
    }
}
