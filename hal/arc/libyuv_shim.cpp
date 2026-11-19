// Minimal libyuv implementation for the AOSP camera HAL's arc/ directory.
// Only the formats the virtual camera uses are implemented for real:
//   I420/YU12, YV12, NV12, NV21, YUY2 -> I420, I420 -> ARGB/ABGR
// MJPEG decoding is NOT implemented (returns -1); the HAL must not advertise
// MJPEG capture while this shim is in use.
#include <libyuv.h>

#include <cstring>

namespace libyuv {
namespace {

inline uint8_t Clamp(int v) {
  return static_cast<uint8_t>(v < 0 ? 0 : (v > 255 ? 255 : v));
}

inline int CopyPlane(const uint8_t* src, int src_stride, uint8_t* dst,
                     int dst_stride, int width, int height) {
  if (dst_stride == width) {
    memcpy(dst, src, static_cast<size_t>(width) * height);
  } else {
    for (int y = 0; y < height; ++y) {
      memcpy(dst + static_cast<size_t>(y) * dst_stride,
             src + static_cast<size_t>(y) * src_stride, width);
    }
  }
  return 0;
}

// Box-average (or nearest for upscaling) resample of a single plane.
void ResampleNearest(const uint8_t* src, int src_stride, uint8_t* dst,
                     int dst_stride, int src_w, int src_h, int dst_w,
                     int dst_h) {
  for (int y = 0; y < dst_h; ++y) {
    int sy = static_cast<int>(static_cast<int64_t>(y) * src_h / dst_h);
    if (sy >= src_h) sy = src_h - 1;
    const uint8_t* src_row = src + static_cast<size_t>(sy) * src_stride;
    uint8_t* dst_row = dst + static_cast<size_t>(y) * dst_stride;
    for (int x = 0; x < dst_w; ++x) {
      int sx = static_cast<int>(static_cast<int64_t>(x) * src_w / dst_w);
      if (sx >= src_w) sx = src_w - 1;
      dst_row[x] = src_row[sx];
    }
  }
}

// Convert NV12/NV21 interleaved chroma to I420 planar.
void InterleavedToI420(const uint8_t* src_y, int src_stride_y,
                       const uint8_t* src_uv, int src_stride_uv,
                       uint8_t* dst_y, int dst_stride_y, uint8_t* dst_u,
                       int dst_stride_u, uint8_t* dst_v, int dst_stride_v,
                       int width, int height, bool uv_swap) {
  CopyPlane(src_y, src_stride_y, dst_y, dst_stride_y, width, height);
  for (int y = 0; y < height / 2; ++y) {
    const uint8_t* uv = src_uv + static_cast<size_t>(y) * src_stride_uv;
    uint8_t* u = dst_u + static_cast<size_t>(y) * dst_stride_u;
    uint8_t* v = dst_v + static_cast<size_t>(y) * dst_stride_v;
    for (int x = 0; x < width / 2; ++x) {
      uint8_t a = uv[2 * x];
      uint8_t b = uv[2 * x + 1];
      if (uv_swap) {
        u[x] = b;
        v[x] = a;
      } else {
        u[x] = a;
        v[x] = b;
      }
    }
  }
}

}  // namespace

int I420Copy(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
             int src_stride_u, const uint8_t* src_v, int src_stride_v,
             uint8_t* dst_y, int dst_stride_y, uint8_t* dst_u,
             int dst_stride_u, uint8_t* dst_v, int dst_stride_v, int width,
             int height) {
  if (!src_y || !dst_y || width <= 0 || height <= 0) return -1;
  CopyPlane(src_y, src_stride_y, dst_y, dst_stride_y, width, height);
  CopyPlane(src_u, src_stride_u, dst_u, dst_stride_u, (width + 1) / 2,
            (height + 1) / 2);
  CopyPlane(src_v, src_stride_v, dst_v, dst_stride_v, (width + 1) / 2,
            (height + 1) / 2);
  return 0;
}

int I420Scale(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
              int src_stride_u, const uint8_t* src_v, int src_stride_v,
              int src_width, int src_height, uint8_t* dst_y, int dst_stride_y,
              uint8_t* dst_u, int dst_stride_u, uint8_t* dst_v,
              int dst_stride_v, int dst_width, int dst_height,
              enum FilterMode /*filtering*/) {
  if (!src_y || !dst_y || src_width <= 0 || src_height <= 0 ||
      dst_width <= 0 || dst_height <= 0) {
    return -1;
  }
  ResampleNearest(src_y, src_stride_y, dst_y, dst_stride_y, src_width,
                  src_height, dst_width, dst_height);
  ResampleNearest(src_u, src_stride_u, dst_u, dst_stride_u, (src_width + 1) / 2,
                  (src_height + 1) / 2, (dst_width + 1) / 2,
                  (dst_height + 1) / 2);
  ResampleNearest(src_v, src_stride_v, dst_v, dst_stride_v, (src_width + 1) / 2,
                  (src_height + 1) / 2, (dst_width + 1) / 2,
                  (dst_height + 1) / 2);
  return 0;
}

int YUY2ToI420(const uint8_t* src_yuy2, int src_stride_yuy2, uint8_t* dst_y,
               int dst_stride_y, uint8_t* dst_u, int dst_stride_u,
               uint8_t* dst_v, int dst_stride_v, int width, int height) {
  if (!src_yuy2 || !dst_y || width <= 0 || height <= 0) return -1;

  for (int y = 0; y < height; ++y) {
    const uint8_t* src_row = src_yuy2 + static_cast<size_t>(y) * src_stride_yuy2;
    uint8_t* y_row = dst_y + static_cast<size_t>(y) * dst_stride_y;
    for (int x = 0; x + 1 < width; x += 2) {
      y_row[x] = src_row[2 * x];
      y_row[x + 1] = src_row[2 * x + 2];
    }
    if (width & 1) y_row[width - 1] = src_row[2 * (width - 1)];

    if ((y & 1) == 0 && (y / 2) < (height + 1) / 2) {
      uint8_t* u_row = dst_u + static_cast<size_t>(y / 2) * dst_stride_u;
      uint8_t* v_row = dst_v + static_cast<size_t>(y / 2) * dst_stride_v;
      for (int x = 0; x + 1 < width; x += 2) {
        u_row[x / 2] = src_row[2 * x + 1];
        v_row[x / 2] = src_row[2 * x + 3];
      }
    }
  }
  return 0;
}

namespace {

void I420ToRGBA(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
                int src_stride_u, const uint8_t* src_v, int src_stride_v,
                uint8_t* dst, int dst_stride, int width, int height,
                bool bgr_order) {
  for (int y = 0; y < height; ++y) {
    const uint8_t* y_row = src_y + static_cast<size_t>(y) * src_stride_y;
    const uint8_t* u_row = src_u + static_cast<size_t>(y / 2) * src_stride_u;
    const uint8_t* v_row = src_v + static_cast<size_t>(y / 2) * src_stride_v;
    uint8_t* out = dst + static_cast<size_t>(y) * dst_stride;
    for (int x = 0; x < width; ++x) {
      int yy = 1164 * (static_cast<int>(y_row[x]) - 16) / 1000;
      int uu = static_cast<int>(u_row[x / 2]) - 128;
      int vv = static_cast<int>(v_row[x / 2]) - 128;
      int r = yy + (1596 * vv) / 1000;
      int g = yy - (813 * vv) / 1000 - (391 * uu) / 1000;
      int b = yy + (2018 * uu) / 1000;
      if (bgr_order) {
        out[0] = Clamp(b);
        out[1] = Clamp(g);
        out[2] = Clamp(r);
      } else {
        out[0] = Clamp(r);
        out[1] = Clamp(g);
        out[2] = Clamp(b);
      }
      out[3] = 255;
      out += 4;
    }
  }
}

}  // namespace

int I420ToARGB(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
               int src_stride_u, const uint8_t* src_v, int src_stride_v,
               uint8_t* dst_argb, int dst_stride_argb, int width, int height) {
  if (!src_y || !dst_argb) return -1;
  I420ToRGBA(src_y, src_stride_y, src_u, src_stride_u, src_v, src_stride_v,
             dst_argb, dst_stride_argb, width, height, /*bgr_order=*/true);
  return 0;
}

int I420ToABGR(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
               int src_stride_u, const uint8_t* src_v, int src_stride_v,
               uint8_t* dst_abgr, int dst_stride_abgr, int width, int height) {
  if (!src_y || !dst_abgr) return -1;
  I420ToRGBA(src_y, src_stride_y, src_u, src_stride_u, src_v, src_stride_v,
             dst_abgr, dst_stride_abgr, width, height, /*bgr_order=*/false);
  return 0;
}

int MJPGToI420(const uint8_t* /*sample*/, size_t /*sample_size*/,
               uint8_t* /*dst_y*/, int /*dst_stride_y*/, uint8_t* /*dst_u*/,
               int /*dst_stride_u*/, uint8_t* /*dst_v*/, int /*dst_stride_v*/,
               int /*src_width*/, int /*src_height*/, int /*dst_width*/,
               int /*dst_height*/) {
  // Requires a JPEG decoder; deliberately unsupported in this shim.
  return -1;
}

int ConvertToI420(const uint8_t* sample, size_t sample_size, uint8_t* dst_y,
                  int dst_stride_y, uint8_t* dst_u, int dst_stride_u,
                  uint8_t* dst_v, int dst_stride_v, int crop_x, int crop_y,
                  int src_width, int src_height, int crop_width,
                  int crop_height, enum RotationMode rotation, uint32_t fourcc) {
  if (!sample || !dst_y || crop_width <= 0 || crop_height <= 0) return -1;
  if (crop_x < 0 || crop_y < 0 || crop_x + crop_width > src_width ||
      crop_y + crop_height > src_height) {
    return -1;
  }

  const int cw = crop_width;
  const int ch = crop_height;
  const int cstride = cw;
  uint8_t* buf = new uint8_t[static_cast<size_t>(cstride) * ch * 3 / 2];
  uint8_t* c_y = buf;
  uint8_t* c_u = buf + static_cast<size_t>(cstride) * ch;
  uint8_t* c_v = c_u + static_cast<size_t>(cstride / 2) * (ch / 2);
  const int cstride_uv = cw / 2;

  int ret = 0;
  const uint32_t f = fourcc ? fourcc : LIBYUV_FOURCC('I', '4', '2', '0');
  if (f == LIBYUV_FOURCC('I', '4', '2', '0') ||
      f == LIBYUV_FOURCC('Y', 'U', '1', '2') ||
      f == LIBYUV_FOURCC('Y', 'V', '1', '2')) {
    const bool swap_uv = (f == LIBYUV_FOURCC('Y', 'V', '1', '2'));
    const uint8_t* sp_y = sample + static_cast<size_t>(crop_y) * src_width + crop_x;
    const uint8_t* sp_u = sample + static_cast<size_t>(src_width) * src_height;
    const uint8_t* sp_v = sp_u + static_cast<size_t>(src_width / 2) * (src_height / 2);
    if (swap_uv) {
      const uint8_t* t = sp_u;
      sp_u = sp_v;
      sp_v = t;
    }
    for (int y = 0; y < ch; ++y) {
      const uint8_t* r = sp_y + static_cast<size_t>(y) * src_width;
      if (y & 1) {
        memcpy(c_y + static_cast<size_t>(y) * cstride, r, cw);
      } else {
        memcpy(c_y + static_cast<size_t>(y) * cstride, r, cw);
        memcpy(c_u + static_cast<size_t>(y / 2) * cstride_uv,
               sp_u + static_cast<size_t>(crop_y / 2 + y / 2) * (src_width / 2) +
                   crop_x / 2,
               cw / 2);
        memcpy(c_v + static_cast<size_t>(y / 2) * cstride_uv,
               sp_v + static_cast<size_t>(crop_y / 2 + y / 2) * (src_width / 2) +
                   crop_x / 2,
               cw / 2);
      }
    }
  } else if (f == LIBYUV_FOURCC('N', 'V', '1', '2') ||
             f == LIBYUV_FOURCC('N', 'V', '2', '1')) {
    const uint8_t* sp_uv =
        sample + static_cast<size_t>(src_width) * src_height;
    InterleavedToI420(sample + static_cast<size_t>(crop_y) * src_width + crop_x,
                      src_width, sp_uv, src_width, c_y, cstride, c_u,
                      cstride_uv, c_v, cstride_uv, cw, ch,
                      f == LIBYUV_FOURCC('N', 'V', '2', '1'));
  } else if (f == LIBYUV_FOURCC('Y', 'U', 'Y', '2')) {
    ret = YUY2ToI420(sample + static_cast<size_t>(crop_y) * src_width * 2 +
                         crop_x * 2,
                     src_width * 2, c_y, cstride, c_u, cstride_uv, c_v,
                     cstride_uv, cw, ch);
  } else {
    delete[] buf;
    return -1;
  }

  if (ret == 0) {
    const uint32_t src_bytes = static_cast<uint32_t>(sample_size);
    (void)src_bytes;
    const int dst_w = (rotation == kRotate90 || rotation == kRotate270) ? ch : cw;
    const int dst_h = (rotation == kRotate90 || rotation == kRotate270) ? cw : ch;
    const int dstride_uv = dst_w / 2;
    uint8_t* d_u = dst_u;
    uint8_t* d_v = dst_v;
    if (d_u == nullptr || d_v == nullptr) {
      delete[] buf;
      return -1;
    }
    for (int y = 0; y < ch; ++y) {
      for (int x = 0; x < cw; ++x) {
        uint8_t yv = c_y[static_cast<size_t>(y) * cstride + x];
        switch (rotation) {
          case kRotate90:
            dst_y[static_cast<size_t>(x) * dst_stride_y + (ch - 1 - y)] = yv;
            break;
          case kRotate180:
            dst_y[static_cast<size_t>(ch - 1 - y) * dst_stride_y + (cw - 1 - x)] = yv;
            break;
          case kRotate270:
            dst_y[static_cast<size_t>(cw - 1 - x) * dst_stride_y + y] = yv;
            break;
          default:
            dst_y[static_cast<size_t>(y) * dst_stride_y + x] = yv;
            break;
        }
      }
    }
    for (int y = 0; y < ch / 2; ++y) {
      for (int x = 0; x < cw / 2; ++x) {
        uint8_t uv = c_u[static_cast<size_t>(y) * cstride_uv + x];
        uint8_t vv = c_v[static_cast<size_t>(y) * cstride_uv + x];
        int dx, dy;
        switch (rotation) {
          case kRotate90:
            dx = x;
            dy = (ch / 2) - 1 - y;
            break;
          case kRotate180:
            dx = (cw / 2) - 1 - x;
            dy = (ch / 2) - 1 - y;
            break;
          case kRotate270:
            dx = (cw / 2) - 1 - x;
            dy = y;
            break;
          default:
            dx = x;
            dy = y;
            break;
        }
        d_u[static_cast<size_t>(dy) * dstride_uv + dx] = uv;
        d_v[static_cast<size_t>(dy) * dstride_uv + dx] = vv;
      }
    }
    (void)dst_h;
  }

  delete[] buf;
  return ret;
}

}  // namespace libyuv
