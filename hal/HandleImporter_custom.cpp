// Custom HandleImporter: bypasses HIDL mapper, uses legacy gralloc module
#include "HandleImporter.h"

#include <hardware/gralloc.h>
#include <hardware/hardware.h>
#include <log/log.h>
#include <unistd.h>

namespace android {
namespace hardware {
namespace camera {
namespace common {
namespace V1_0 {
namespace helper {

using android::hardware::graphics::mapper::V2_0::IMapper;
using android::hardware::graphics::mapper::V2_0::YCbCrLayout;

HandleImporter::HandleImporter() {}

bool HandleImporter::importBuffer(buffer_handle_t& handle) { return handle != nullptr; }

void HandleImporter::freeBuffer(buffer_handle_t) {}

bool HandleImporter::importFence(const native_handle_t* handle, int& fd) const {
    if (handle == nullptr || handle->numFds == 0) { fd = -1; return true; }
    fd = dup(handle->data[0]);
    return fd >= 0;
}

void HandleImporter::closeFence(int fd) const { if (fd >= 0) close(fd); }

static gralloc_module_t const* getGralloc() {
    gralloc_module_t const* m = nullptr;
    if (hw_get_module(GRALLOC_HARDWARE_MODULE_ID,
                      reinterpret_cast<const hw_module_t**>(&m)) != 0) return nullptr;
    return m;
}

void* HandleImporter::lock(buffer_handle_t& buf, uint64_t cpuUsage, size_t) {
    gralloc_module_t const* m = getGralloc();
    if (!m || !m->lock) return nullptr;
    void* base = nullptr;
    if (m->lock(m, buf, cpuUsage, 0, 0, 0, 0, &base) != 0) return nullptr;
    return base;
}

void* HandleImporter::lock(buffer_handle_t& buf, uint64_t cpuUsage, const IMapper::Rect& r) {
    gralloc_module_t const* m = getGralloc();
    if (!m || !m->lock) return nullptr;
    void* base = nullptr;
    if (m->lock(m, buf, cpuUsage, r.left, r.top, r.width, r.height, &base) != 0) return nullptr;
    return base;
}

YCbCrLayout HandleImporter::lockYCbCr(buffer_handle_t& buf, uint64_t cpuUsage,
                                     const IMapper::Rect& r) {
    YCbCrLayout out = {};
    gralloc_module_t const* m = getGralloc();
    if (!m || !m->lock_ycbcr) { ALOGE("G98HI no gralloc lock_ycbcr"); return out; }
    android_ycbcr y = {};
    int rc = m->lock_ycbcr(m, buf, cpuUsage, r.left, r.top, r.width, r.height, &y);
    if (rc != 0) { ALOGE("G98HI lock_ycbcr rc=%d", rc); return out; }
    out.y = y.y; out.cb = y.cb; out.cr = y.cr;
    out.yStride = y.ystride; out.cStride = y.cstride; out.chromaStep = y.chroma_step;
    ALOGV("G98HI lock_ycbcr ok y=%p", out.y);
    return out;
}

status_t HandleImporter::getMonoPlanarStrideBytes(buffer_handle_t&, uint32_t* stride) {
    if (stride) *stride = 0;
    return android::INVALID_OPERATION;
}

int HandleImporter::unlock(buffer_handle_t& buf) {
    gralloc_module_t const* m = getGralloc();
    if (m && m->unlock) m->unlock(m, buf);
    return -1;
}



}  // helper
}  // V1_0
}  // common
}  // camera
}  // hardware
}  // android