/* Copyright 2017 The Chromium OS Authors. All rights reserved.
 * Use of this source code is governed by a BSD-style license that can be
 * found in the LICENSE file.
 */

#include "arc/frame_buffer.h"

#include <cstdio>
#include <utility>
#include <sstream>

#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>
#include "arc/common.h"
#include "arc/image_processor.h"

namespace arc {

FrameBuffer::FrameBuffer()
    : data_(nullptr),
      data_size_(0),
      buffer_size_(0),
      width_(0),
      height_(0),
      fourcc_(0) {}

FrameBuffer::~FrameBuffer() {}

int FrameBuffer::SetDataSize(size_t data_size) {
  if (data_size > buffer_size_) {
    LOGF(ERROR) << "Buffer overflow: Buffer only has " << buffer_size_
                << ", but data needs " << data_size;
    return -EINVAL;
  }
  data_size_ = data_size;
  return 0;
}

AllocatedFrameBuffer::AllocatedFrameBuffer(int buffer_size) {
  buffer_.reset(new uint8_t[buffer_size]);
  buffer_size_ = buffer_size;
  data_ = buffer_.get();
}

AllocatedFrameBuffer::AllocatedFrameBuffer(uint8_t* buffer, int buffer_size) {
  buffer_.reset(buffer);
  buffer_size_ = buffer_size;
  data_ = buffer_.get();
}

AllocatedFrameBuffer::~AllocatedFrameBuffer() {}

int AllocatedFrameBuffer::SetDataSize(size_t size) {
  if (size > buffer_size_) {
    buffer_.reset(new uint8_t[size]);
    buffer_size_ = size;
    data_ = buffer_.get();
  }
  data_size_ = size;
  return 0;
}

void AllocatedFrameBuffer::Reset() { memset(data_, 0, buffer_size_); }

V4L2FrameBuffer::V4L2FrameBuffer(base::ScopedFD fd, int buffer_size,
                                 uint32_t width, uint32_t height,
                                 uint32_t fourcc)
    : fd_(std::move(fd)), is_mapped_(false) {
  buffer_size_ = buffer_size;
  width_ = width;
  height_ = height;
  fourcc_ = fourcc;
}

V4L2FrameBuffer::~V4L2FrameBuffer() {
  if (Unmap()) {
    LOGF(ERROR) << "Unmap failed";
  }
}

int V4L2FrameBuffer::Map() {
  base::AutoLock l(lock_);
  if (is_mapped_) {
    LOGF(ERROR) << "The buffer is already mapped";
    return -EINVAL;
  }
  void* addr = mmap(NULL, buffer_size_, PROT_READ, MAP_SHARED, fd_.get(), 0);
  if (addr == MAP_FAILED) {
    LOGF(ERROR) << "mmap() failed: " << strerror(errno);
    return -EINVAL;
  }
  data_ = static_cast<uint8_t*>(addr);
  is_mapped_ = true;
  return 0;
}

int V4L2FrameBuffer::Unmap() {
  base::AutoLock l(lock_);
  if (is_mapped_ && munmap(data_, buffer_size_)) {
    LOGF(ERROR) << "mummap() failed: " << strerror(errno);
    return -EINVAL;
  }
  is_mapped_ = false;
  return 0;
}

GrallocFrameBuffer::GrallocFrameBuffer(buffer_handle_t buffer, uint32_t width,
                                       uint32_t height, uint32_t fourcc,
                                       uint32_t device_buffer_length,
                                       uint32_t stream_usage)
    : owned_handle_(buffer ? native_handle_clone(buffer) : nullptr),
      buffer_(owned_handle_ ? owned_handle_ : buffer),
      is_mapped_(false),
      direct_mapped_(false),
      device_buffer_length_(device_buffer_length),
      stream_usage_(stream_usage) {
  const hw_module_t* module = nullptr;
  int ret = hw_get_module(GRALLOC_HARDWARE_MODULE_ID, &module);
  if (ret || !module) {
    LOGF(ERROR) << "Failed to get gralloc module.";
    return;
  }
  gralloc_module_ = reinterpret_cast<const gralloc_module_t*>(module);
  width_ = width;
  height_ = height;
  fourcc_ = fourcc;
}

GrallocFrameBuffer::~GrallocFrameBuffer() {
  if (Unmap()) {
    LOGF(ERROR) << "Unmap failed";
  }
  if (owned_handle_) {
    native_handle_delete(owned_handle_);
    owned_handle_ = nullptr;
  }
}

int GrallocFrameBuffer::Map() {
  base::AutoLock l(lock_);
  if (is_mapped_) {
    LOGF(ERROR) << "The buffer is already mapped";
    return -EINVAL;
  }

  // The camera3 buffers handed to this HAL are foreign handles that the
  // redroid/g98 gralloc refuses to lock. They do carry a dma-buf fd, so map
  // that fd directly instead of going through the gralloc module.
  if (buffer_ && buffer_->numFds >= 1 && buffer_->data[0] >= 0) {
    size_t map_size = ImageProcessor::GetConvertedSize(fourcc_, width_, height_);
    if (map_size == 0) {
      map_size = device_buffer_length_;
    }
    if (map_size > 0) {
      const int fd = buffer_->data[0];
      const int flags = fcntl(fd, F_GETFL);
      void* direct = mmap(nullptr, map_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
      if (direct == MAP_FAILED) {
        const int first_errno = errno;
        char path[64];
        snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
        int fd_rw = open(path, O_RDWR | O_CLOEXEC);
        const int reopen_errno = errno;
        if (fd_rw >= 0) {
          direct = mmap(nullptr, map_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd_rw, 0);
          const int remap_errno = errno;
          close(fd_rw);
          if (direct == MAP_FAILED) {
            LOGF(ERROR) << "Map(): reopen O_RDWR mmap failed: " << strerror(remap_errno);
          }
        }
        if (direct == MAP_FAILED) {
          LOGF(ERROR) << "Map(): Direct mmap of dma-buf failed: " << strerror(first_errno)
                      << " fd=" << fd << " fcntl=0x" << std::hex << flags << std::dec
                      << " accmode=" << (flags & O_ACCMODE)
                      << " reopen=" << strerror(reopen_errno) << " size=" << map_size
                      << " fourcc=0x" << std::hex << fourcc_ << std::dec;
        }
      }
      if (direct != MAP_FAILED) {
        data_ = static_cast<uint8_t*>(direct);
        buffer_size_ = map_size;
        direct_mapped_ = true;
        is_mapped_ = true;
        return 0;
      }
    }
  }

  void* addr = nullptr;
  int ret = 0;
  switch (fourcc_) {
    case V4L2_PIX_FMT_YUV420:
    case V4L2_PIX_FMT_YVU420:
    case V4L2_PIX_FMT_YUYV: {
      android_ycbcr yuv_data = {};
      ret = gralloc_module_->lock_ycbcr(gralloc_module_, buffer_, stream_usage_,
                                        0, 0, width_, height_, &yuv_data);
      addr = yuv_data.y;
      break;
    }
    case V4L2_PIX_FMT_JPEG:
      ret = gralloc_module_->lock(gralloc_module_, buffer_, stream_usage_, 0, 0,
                                  device_buffer_length_, 1, &addr);
      break;
    case V4L2_PIX_FMT_BGR32:
    case V4L2_PIX_FMT_RGB32:
      ret = gralloc_module_->lock(gralloc_module_, buffer_, stream_usage_, 0, 0,
                                  width_, height_, &addr);
      break;
    default:
      return -EINVAL;
  }

  if (ret) {
    LOGF(ERROR) << "Failed to gralloc lock buffer: " << ret
                << " numFds=" << (buffer_ ? buffer_->numFds : -1)
                << " numInts=" << (buffer_ ? buffer_->numInts : -1);
    return ret;
  }

  data_ = static_cast<uint8_t*>(addr);
  if (fourcc_ == V4L2_PIX_FMT_YVU420 || fourcc_ == V4L2_PIX_FMT_YUV420 ||
      fourcc_ == V4L2_PIX_FMT_NV21 || fourcc_ == V4L2_PIX_FMT_RGB32 ||
      fourcc_ == V4L2_PIX_FMT_BGR32) {
    buffer_size_ = ImageProcessor::GetConvertedSize(fourcc_, width_, height_);
  }

  is_mapped_ = true;
  return 0;
}

int GrallocFrameBuffer::Unmap() {
  base::AutoLock l(lock_);
  if (!is_mapped_) {
    return 0;
  }
  if (direct_mapped_) {
    if (munmap(data_, buffer_size_)) {
      LOGF(ERROR) << "munmap() failed: " << strerror(errno);
      return -EINVAL;
    }
  } else if (gralloc_module_->unlock(gralloc_module_, buffer_)) {
    LOGF(ERROR) << "Failed to unmap buffer: ";
    return -EINVAL;
  }
  is_mapped_ = false;
  direct_mapped_ = false;
  return 0;
}

}  // namespace arc
