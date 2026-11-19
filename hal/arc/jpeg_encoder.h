// Self-contained JPEG encoder for the arc camera HAL.
// Wraps the public-domain jo_jpeg encoder (a13arc/jo_jpeg.cpp) so that an I420
// frame can be encoded straight into a client buffer.
#pragma once
#include <cstddef>
#include <cstdint>

// Encodes an I420 (a.k.a. YU12) image into |out| as a baseline JPEG.
// Returns the number of bytes written, or -1 on failure. |out_capacity| is the
// size of |out|; encoding fails if the result would not fit.
int EncodeJpegI420(const uint8_t* y, int stride_y, const uint8_t* u,
                   int stride_u, const uint8_t* v, int stride_v, int width,
                   int height, int quality, uint8_t* out,
                   size_t out_capacity);
