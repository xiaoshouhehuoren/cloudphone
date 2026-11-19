// Minimal libyuv shim for the AOSP camera HAL's arc/ directory.
// Provides only the API surface used by arc/{image_processor,cached_frame}.cpp.
#pragma once
#include <cstddef>
#include <cstdint>

namespace libyuv {

#define LIBYUV_FOURCC(a, b, c, d) \
  ((static_cast<uint32_t>(a)) | ((static_cast<uint32_t>(b)) << 8) | \
   ((static_cast<uint32_t>(c)) << 16) | ((static_cast<uint32_t>(d)) << 24))

enum FourCC {
  FOURCC_I420 = LIBYUV_FOURCC('I', '4', '2', '0'),
  FOURCC_YU12 = LIBYUV_FOURCC('Y', 'U', '1', '2'),
  FOURCC_YV12 = LIBYUV_FOURCC('Y', 'V', '1', '2'),
  FOURCC_NV12 = LIBYUV_FOURCC('N', 'V', '1', '2'),
  FOURCC_NV21 = LIBYUV_FOURCC('N', 'V', '2', '1'),
  FOURCC_YUY2 = LIBYUV_FOURCC('Y', 'U', 'Y', '2'),
  FOURCC_MJPG = LIBYUV_FOURCC('M', 'J', 'P', 'G'),
};

enum FilterMode {
  kFilterNone = 0,
  kFilterLinear = 1,
  kFilterBilinear = 2,
  kFilterBox = 3,
};

enum RotationMode {
  kRotate0 = 0,
  kRotate90 = 90,
  kRotate180 = 180,
  kRotate270 = 270,
};

int I420Copy(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
             int src_stride_u, const uint8_t* src_v, int src_stride_v,
             uint8_t* dst_y, int dst_stride_y, uint8_t* dst_u,
             int dst_stride_u, uint8_t* dst_v, int dst_stride_v, int width,
             int height);

int I420Scale(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
              int src_stride_u, const uint8_t* src_v, int src_stride_v,
              int src_width, int src_height, uint8_t* dst_y, int dst_stride_y,
              uint8_t* dst_u, int dst_stride_u, uint8_t* dst_v,
              int dst_stride_v, int dst_width, int dst_height,
              enum FilterMode filtering);

int YUY2ToI420(const uint8_t* src_yuy2, int src_stride_yuy2, uint8_t* dst_y,
               int dst_stride_y, uint8_t* dst_u, int dst_stride_u,
               uint8_t* dst_v, int dst_stride_v, int width, int height);

int I420ToARGB(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
               int src_stride_u, const uint8_t* src_v, int src_stride_v,
               uint8_t* dst_argb, int dst_stride_argb, int width, int height);

int I420ToABGR(const uint8_t* src_y, int src_stride_y, const uint8_t* src_u,
               int src_stride_u, const uint8_t* src_v, int src_stride_v,
               uint8_t* dst_abgr, int dst_stride_abgr, int width, int height);

int MJPGToI420(const uint8_t* sample, size_t sample_size, uint8_t* dst_y,
               int dst_stride_y, uint8_t* dst_u, int dst_stride_u,
               uint8_t* dst_v, int dst_stride_v, int src_width, int src_height,
               int dst_width, int dst_height);

int ConvertToI420(const uint8_t* sample, size_t sample_size, uint8_t* dst_y,
                  int dst_stride_y, uint8_t* dst_u, int dst_stride_u,
                  uint8_t* dst_v, int dst_stride_v, int crop_x, int crop_y,
                  int src_width, int src_height, int crop_width,
                  int crop_height, enum RotationMode rotation, uint32_t fourcc);

}  // namespace libyuv
