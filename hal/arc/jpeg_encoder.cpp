#include "jpeg_encoder.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

#include "libyuv.h"

// Defined in jo_jpeg.cpp (public-domain encoder by Jon Olick).
bool jo_write_jpg_fp(FILE* fp, const void* data, int width, int height,
                     int comp, int quality);

int EncodeJpegI420(const uint8_t* y, int stride_y, const uint8_t* u,
                   int stride_u, const uint8_t* v, int stride_v, int width,
                   int height, int quality, uint8_t* out,
                   size_t out_capacity) {
  if (!y || !u || !v || !out || width <= 0 || height <= 0) return -1;
  if (quality < 1 || quality > 100) quality = 90;

  // jo_jpeg takes packed RGB(A) pixels, so convert I420 -> RGBA first.
  std::vector<uint8_t> rgba(static_cast<size_t>(width) * height * 4);
  int res = libyuv::I420ToABGR(y, stride_y, u, stride_u, v, stride_v,
                               rgba.data(), width * 4, width, height);
  if (res) return -1;

  char* buf = nullptr;
  size_t size = 0;
  FILE* fp = open_memstream(&buf, &size);
  if (!fp) return -1;

  bool ok = jo_write_jpg_fp(fp, rgba.data(), width, height, 4, quality);
  if (fclose(fp) != 0) ok = false;
  if (!ok || size == 0 || size > out_capacity) {
    free(buf);
    return -1;
  }
  memcpy(out, buf, size);
  free(buf);
  return static_cast<int>(size);
}
