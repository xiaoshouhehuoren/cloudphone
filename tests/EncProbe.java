package com.g98.probe;
import android.media.MediaCodec;
import android.media.MediaCodecInfo;
import android.media.MediaCodecList;
import android.media.MediaFormat;
import java.nio.ByteBuffer;

public class EncProbe {
  static void list() {
    for (MediaCodecInfo ci : new MediaCodecList(MediaCodecList.ALL_CODECS).getCodecInfos()) {
      if (!ci.isEncoder()) continue;
      StringBuilder sb = new StringBuilder("ENCODER " + ci.getName());
      for (String t : ci.getSupportedTypes()) {
        if (!t.equalsIgnoreCase("video/avc")) continue;
        try {
          int[] colors = ci.getCapabilitiesForType(t).colorFormats;
          sb.append(" avcColors=");
          for (int c : colors) sb.append(c).append(",");
        } catch (Throwable e) { sb.append(" err"); }
      }
      System.out.println(sb);
    }
  }
  static void test(String name, int w, int h, int color) {
    MediaCodec c = null;
    int frames = 0; long outBytes = 0;
    try {
      c = name.equals("DEFAULT") ? MediaCodec.createEncoderByType("video/avc") : MediaCodec.createByCodecName(name);
      System.out.println("USING " + c.getName());
      MediaFormat f = MediaFormat.createVideoFormat("video/avc", w, h);
      f.setInteger(MediaFormat.KEY_COLOR_FORMAT, color);
      f.setInteger(MediaFormat.KEY_BIT_RATE, 1000000);
      f.setInteger(MediaFormat.KEY_FRAME_RATE, 30);
      f.setInteger(MediaFormat.KEY_I_FRAME_INTERVAL, 1);
      c.configure(f, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE);
      c.start();
      long pts = 0;
      MediaCodec.BufferInfo info = new MediaCodec.BufferInfo();
      long deadline = System.currentTimeMillis() + 4000;
      while (System.currentTimeMillis() < deadline) {
        if (frames < 30) {
          int in = c.dequeueInputBuffer(20000);
          if (in >= 0) {
            ByteBuffer b = c.getInputBuffer(in);
            b.clear();
            int need = w * h * 3 / 2;
            byte[] yuv = new byte[Math.min(need, b.capacity())];
            for (int i = 0; i < w * h && i < yuv.length; i++) yuv[i] = (byte)((i / w + frames * 4) & 0xff);
            for (int i = w * h; i < yuv.length; i++) yuv[i] = (byte)(128 + (frames % 3));
            b.put(yuv);
            c.queueInputBuffer(in, 0, yuv.length, pts, 0);
            pts += 33333; frames++;
          }
        }
        int out = c.dequeueOutputBuffer(info, 20000);
        if (out >= 0) { outBytes += info.size; c.releaseOutputBuffer(out, false); }
        else if (out == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED) System.out.println("FORMAT " + c.getOutputFormat());
      }
      System.out.println("RESULT " + name + " " + w + "x" + h + " color=" + color + " frames=" + frames + " outBytes=" + outBytes + " OK");
    } catch (Throwable e) {
      System.out.println("RESULT " + name + " " + w + "x" + h + " color=" + color + " frames=" + frames + " outBytes=" + outBytes + " FAIL " + e);
    } finally {
      try { if (c != null) { c.stop(); c.release(); } } catch (Throwable ignored) {}
    }
  }
  public static void main(String[] a) {
    String codec = a.length > 0 ? a[0] : "OMX.google.h264.encoder";
    if (codec.equals("list")) { list(); return; }
    test(codec, 448, 640, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420SemiPlanar);
    test(codec, 640, 480, MediaCodecInfo.CodecCapabilities.COLOR_FormatYUV420Planar);
  }
}
