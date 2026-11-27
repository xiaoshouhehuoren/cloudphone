/*
 * Copyright 2016 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#include "capture_request.h"

#include "common.h"

#include <cutils/native_handle.h>
#include <log/log.h>
#include <mutex>
#include <unordered_map>

namespace {
std::mutex g_last_handle_mu;
std::unordered_map<const buffer_handle_t*, native_handle_t*> g_last_handle;
static bool valid_native_handle(const native_handle_t* h) {
  return h && h->version == (int)sizeof(native_handle_t) &&
         h->numFds >= 0 && h->numFds <= 16 &&
         h->numInts >= 0 && h->numInts <= 128;
}
}


namespace default_camera_hal {

CaptureRequest::CaptureRequest() : CaptureRequest(nullptr) {}

CaptureRequest::~CaptureRequest() {
  for (buffer_handle_t handle : owned_output_handles_) {
    if (handle) {
      // native_handle_clone() dup()ed every fd; native_handle_delete() only
      // frees the struct, so the dup'ed dma-buf fds must be closed here or the
      // provider leaks ~one fd per frame until it hits RLIMIT_NOFILE.
      native_handle_close(const_cast<native_handle_t*>(handle));
      native_handle_delete(const_cast<native_handle_t*>(handle));
    }
  }
}

CaptureRequest::CaptureRequest(const camera3_capture_request_t* request) {
  HAL_LOGV("CaptureRequest: CTOR request=%p", request);
  if (!request) {
    return;
  }
  HAL_LOGV("CaptureRequest: CTOR fields req=%p frame=%u nout=%u outs=%p input=%p",
        request, request->frame_number, request->num_output_buffers,
        request->output_buffers, request->input_buffer);
  for (uint32_t i = 0; i < request->num_output_buffers && i < 8; ++i) {
    const camera3_stream_buffer_t& b = request->output_buffers[i];
    const native_handle_t* h = b.buffer ? *b.buffer : nullptr;
    HAL_LOGV("CaptureRequest: CTOR buf[%u] stream=%p bufptr=%p h=%p v=%d fds=%d ints=%d",
          i, b.stream, b.buffer, h, h ? h->version : -1,
          h ? h->numFds : -1, h ? h->numInts : -1);
  }

  frame_number = request->frame_number;

  // CameraMetadata makes copies of camera_metadata_t through the
  // assignment operator (the constructor taking a camera_metadata_t*
  // takes ownership instead).
  settings = request->settings;

  // camera3_stream_buffer_t can be default copy constructed,
  // as its pointer values are handles, not ownerships.

  // Copy the input buffer.
  if (request->input_buffer) {
    input_buffer =
        std::make_unique<camera3_stream_buffer_t>(*request->input_buffer);
  }

  // Safely copy all the output buffers.
  uint32_t num_output_buffers = request->num_output_buffers;
  if (num_output_buffers < 0 || !request->output_buffers) {
    num_output_buffers = 0;
  }
  output_buffers.insert(output_buffers.end(),
                        request->output_buffers,
                        request->output_buffers + num_output_buffers);
  framework_output_handles_.reserve(output_buffers.size());
  for (const auto& output_buffer : output_buffers) {
    framework_output_handles_.push_back(
        output_buffer.buffer ? *output_buffer.buffer : nullptr);
  }

  // The request is used asynchronously after processCaptureRequest returns.
  // Clone each native handle while the framework-owned request is still valid.
  // Keep clone addresses stable, then rewrite buffer pointers only after all
  // clones are stored; this avoids both framework-handle UAF and vector pointer
  // invalidation.
  owned_output_handles_.reserve(output_buffers.size());
  for (const auto& output_buffer : output_buffers) {
    const native_handle_t* incoming =
        output_buffer.buffer ? *output_buffer.buffer : nullptr;
    native_handle_t* clone = incoming ? native_handle_clone(incoming) : nullptr;
    HAL_LOGV("CaptureRequest: clone src=%p v=%d fds=%d ints=%d dst=%p v=%d fds=%d ints=%d",
          incoming, incoming ? incoming->version : -1, incoming ? incoming->numFds : -1,
          incoming ? incoming->numInts : -1, clone, clone ? clone->version : -1,
          clone ? clone->numFds : -1, clone ? clone->numInts : -1);
    owned_output_handles_.push_back(clone);
  }
  for (size_t i = 0; i < output_buffers.size(); ++i) {
    if (owned_output_handles_[i]) {
      output_buffers[i].buffer = &owned_output_handles_[i];
    }
  }
}

void CaptureRequest::RestoreFrameworkOutputHandles() {
  const size_t count = std::min(output_buffers.size(),
                                framework_output_handles_.size());
  for (size_t i = 0; i < count; ++i) {
    output_buffers[i].buffer = &framework_output_handles_[i];
  }
}

}  // namespace default_camera_hal
