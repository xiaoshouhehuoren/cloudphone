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

//#define LOG_NDEBUG 0
#define LOG_TAG "V4L2MetadataFactory"

#include "v4l2_metadata_factory.h"

#include <camera/CameraMetadata.h>
#include "common.h"
#include "format_metadata_factory.h"
#include "metadata/boottime_state_delegate.h"
#include "metadata/control.h"
#include "metadata/enum_converter.h"
#include "metadata/partial_metadata_factory.h"
#include "metadata/property.h"
#include "metadata/scaling_converter.h"

namespace v4l2_camera_hal {

// According to spec, each unit of V4L2_CID_AUTO_EXPOSURE_BIAS is 0.001 EV.
// K50-style AE compensation: 1/6 EV per step, +/-4 EV total.
const camera_metadata_rational_t kAeCompensationUnit = {1, 6};
// According to spec, each unit of V4L2_CID_EXPOSURE_ABSOLUTE is 100 us.
const int64_t kV4L2ExposureTimeStepNs = 100000;
// According to spec, each unit of V4L2_CID_ISO_SENSITIVITY is ISO/1000.
const int32_t kV4L2SensitivityDenominator = 1000;
// Generously allow up to 6MB (the largest size on the RPi Camera is about 5MB).
const size_t kV4L2MaxJpegSize = 6000000;

int GetV4L2Metadata(std::shared_ptr<V4L2Wrapper> device, int id,
                    std::unique_ptr<Metadata>* result) {
  HAL_LOG_ENTER();

  // Open a temporary connection to the device for all the V4L2 querying
  // that will be happening (this could be done for each component individually,
  // but doing it here prevents connecting and disconnecting for each one).
  V4L2Wrapper::Connection temp_connection = V4L2Wrapper::Connection(device);
  if (temp_connection.status()) {
    HAL_LOGE("Failed to connect to device: %d.", temp_connection.status());
    return temp_connection.status();
  }

  // TODO(b/30035628): Add states.

  PartialMetadataSet components;

  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_COLOR_CORRECTION_ABERRATION_MODE,
      ANDROID_COLOR_CORRECTION_AVAILABLE_ABERRATION_MODES,
      {ANDROID_COLOR_CORRECTION_ABERRATION_MODE_FAST,
       ANDROID_COLOR_CORRECTION_ABERRATION_MODE_HIGH_QUALITY},
      {{CAMERA3_TEMPLATE_STILL_CAPTURE,
        ANDROID_COLOR_CORRECTION_ABERRATION_MODE_HIGH_QUALITY},
       {OTHER_TEMPLATES, ANDROID_COLOR_CORRECTION_ABERRATION_MODE_FAST}}));

  // TODO(b/30510395): subcomponents of 3A.
  // In general, default to ON/AUTO since they imply pretty much nothing,
  // while OFF implies guarantees about not hindering performance.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::array<int32_t, 3>>(ANDROID_CONTROL_MAX_REGIONS,
                                           {{/*AE*/ 1, /*AWB*/ 1, /*AF*/ 1}})));
  // The v4l2loopback device exposes no AE bias control, but every real phone
  // advertises a usable compensation range. Report +/-4 EV (24 steps of 1/6 EV)
  // so Camera1 clients calling setExposureCompensation() don't fail.
  components.insert(NoEffectSliderControl<int32_t>(
      ANDROID_CONTROL_AE_EXPOSURE_COMPENSATION,
      ANDROID_CONTROL_AE_COMPENSATION_RANGE,
      -24,
      24,
      {{OTHER_TEMPLATES, 0}}));
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<camera_metadata_rational_t>(
          ANDROID_CONTROL_AE_COMPENSATION_STEP, kAeCompensationUnit)));
  // TODO(b/31021522): Autofocus subcomponent.
  // K50 back camera has PDAF; the front camera is fixed focus. Advertise the
  // matching modes so legacy APIs that call setFocusMode() keep working.
  {
    std::vector<uint8_t> af_modes = {ANDROID_CONTROL_AF_MODE_OFF};
    if (id == 0) {
      af_modes.push_back(ANDROID_CONTROL_AF_MODE_AUTO);
      af_modes.push_back(ANDROID_CONTROL_AF_MODE_CONTINUOUS_PICTURE);
      af_modes.push_back(ANDROID_CONTROL_AF_MODE_CONTINUOUS_VIDEO);
    }
    components.insert(NoEffectMenuControl<uint8_t>(
        ANDROID_CONTROL_AF_MODE, ANDROID_CONTROL_AF_AVAILABLE_MODES, af_modes));
  }
  // TODO(b/31021522): Should read autofocus state from
  // V4L2_CID_AUTO_FOCUS_STATUS bitmask. The framework gets a little more
  // complex than that does; there's a whole state-machine table in
  // the docs (system/media/camera/docs/docs.html).
  components.insert(FixedState<uint8_t>(
      ANDROID_CONTROL_AF_STATE,
      id == 0 ? ANDROID_CONTROL_AF_STATE_FOCUSED_LOCKED
              : ANDROID_CONTROL_AF_STATE_INACTIVE));
  // TODO(b/31022735): Correctly implement AE & AF triggers that
  // actually do something. These no effect triggers are even worse than most
  // of the useless controls in this class, since technically they should
  // revert back to IDLE eventually after START/CANCEL, but for now they won't
  // unless IDLE is requested.
  components.insert(
      NoEffectMenuControl<uint8_t>(ANDROID_CONTROL_AF_TRIGGER,
                                   DO_NOT_REPORT_OPTIONS,
                                   {ANDROID_CONTROL_AF_TRIGGER_IDLE,
                                    ANDROID_CONTROL_AF_TRIGGER_START,
                                    ANDROID_CONTROL_AF_TRIGGER_CANCEL}));
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_AE_PRECAPTURE_TRIGGER,
      DO_NOT_REPORT_OPTIONS,
      {ANDROID_CONTROL_AE_PRECAPTURE_TRIGGER_IDLE,
       ANDROID_CONTROL_AE_PRECAPTURE_TRIGGER_START,
       ANDROID_CONTROL_AE_PRECAPTURE_TRIGGER_CANCEL}));
  components.insert(V4L2ControlOrDefault<uint8_t>(
      ControlType::kMenu,
      ANDROID_CONTROL_AE_ANTIBANDING_MODE,
      ANDROID_CONTROL_AE_AVAILABLE_ANTIBANDING_MODES,
      device,
      V4L2_CID_POWER_LINE_FREQUENCY,
      std::shared_ptr<ConverterInterface<uint8_t, int32_t>>(
          new EnumConverter({{V4L2_CID_POWER_LINE_FREQUENCY_DISABLED,
                              ANDROID_CONTROL_AE_ANTIBANDING_MODE_OFF},
                             {V4L2_CID_POWER_LINE_FREQUENCY_50HZ,
                              ANDROID_CONTROL_AE_ANTIBANDING_MODE_50HZ},
                             {V4L2_CID_POWER_LINE_FREQUENCY_60HZ,
                              ANDROID_CONTROL_AE_ANTIBANDING_MODE_60HZ},
                             {V4L2_CID_POWER_LINE_FREQUENCY_AUTO,
                              ANDROID_CONTROL_AE_ANTIBANDING_MODE_AUTO}})),
      ANDROID_CONTROL_AE_ANTIBANDING_MODE_AUTO,
      {{CAMERA3_TEMPLATE_MANUAL, ANDROID_CONTROL_AE_ANTIBANDING_MODE_OFF},
       {OTHER_TEMPLATES, ANDROID_CONTROL_AE_ANTIBANDING_MODE_AUTO}}));
  std::unique_ptr<PartialMetadataInterface> exposure_time =
      V4L2Control<int64_t>(ControlType::kSlider,
                           ANDROID_SENSOR_EXPOSURE_TIME,
                           ANDROID_SENSOR_INFO_EXPOSURE_TIME_RANGE,
                           device,
                           V4L2_CID_EXPOSURE_ABSOLUTE,
                           std::make_shared<ScalingConverter<int64_t, int32_t>>(
                               kV4L2ExposureTimeStepNs, 1));
  // TODO(b/31037072): Sensitivity has additional V4L2 controls
  // (V4L2_CID_ISO_SENSITIVITY_AUTO), so this control currently has
  // undefined behavior.
  // TODO(b/30921166): V4L2_CID_ISO_SENSITIVITY is an int menu, so
  // this will return nullptr until that is added.
  std::unique_ptr<PartialMetadataInterface> sensitivity =
      V4L2Control<int32_t>(ControlType::kSlider,
                           ANDROID_SENSOR_SENSITIVITY,
                           ANDROID_SENSOR_INFO_SENSITIVITY_RANGE,
                           device,
                           V4L2_CID_ISO_SENSITIVITY,
                           std::make_shared<ScalingConverter<int32_t, int32_t>>(
                               1, kV4L2SensitivityDenominator));
  std::multimap<int32_t, uint8_t> ae_mode_mapping = {
      {V4L2_EXPOSURE_AUTO, ANDROID_CONTROL_AE_MODE_ON}};
  if (exposure_time && sensitivity) {
    // TODO(b/30510395): as part of coordinated 3A component,
    // if these aren't available don't advertise AE mode OFF, only AUTO.
    components.insert(std::move(exposure_time));
    components.insert(std::move(sensitivity));
    ae_mode_mapping.emplace(V4L2_EXPOSURE_MANUAL, ANDROID_CONTROL_AE_MODE_OFF);
  }
  components.insert(V4L2ControlOrDefault<uint8_t>(
      ControlType::kMenu,
      ANDROID_CONTROL_AE_MODE,
      ANDROID_CONTROL_AE_AVAILABLE_MODES,
      device,
      V4L2_CID_EXPOSURE_AUTO,
      std::shared_ptr<ConverterInterface<uint8_t, int32_t>>(
          new EnumConverter(ae_mode_mapping)),
      ANDROID_CONTROL_AE_MODE_ON,
      {{CAMERA3_TEMPLATE_MANUAL, ANDROID_CONTROL_AE_MODE_OFF},
       {OTHER_TEMPLATES, ANDROID_CONTROL_AE_MODE_ON}}));
  // Can't get AE status from V4L2.
  // TODO(b/30510395): If AE mode is OFF, this should switch to INACTIVE.
  components.insert(FixedState<uint8_t>(ANDROID_CONTROL_AE_STATE,
                                        ANDROID_CONTROL_AE_STATE_CONVERGED));
  // V4L2 offers multiple white balance interfaces. Try the advanced one before
  // falling
  // back to the simpler version.
  // Modes from each API that don't match up:
  // Android: WARM_FLUORESCENT, TWILIGHT.
  // V4L2: FLUORESCENT_H, HORIZON, FLASH.
  // The v4l2 loopback has no white balance control, but real Camera1 apps
  // (WeChat/YouTu) expect the usual preset list and call setWhiteBalance() with
  // them. Advertise the standard presets; they have no effect on the stream.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_AWB_MODE, ANDROID_CONTROL_AWB_AVAILABLE_MODES,
      {ANDROID_CONTROL_AWB_MODE_AUTO, ANDROID_CONTROL_AWB_MODE_INCANDESCENT,
       ANDROID_CONTROL_AWB_MODE_FLUORESCENT, ANDROID_CONTROL_AWB_MODE_DAYLIGHT,
       ANDROID_CONTROL_AWB_MODE_CLOUDY_DAYLIGHT,
       ANDROID_CONTROL_AWB_MODE_SHADE},
      {{OTHER_TEMPLATES, ANDROID_CONTROL_AWB_MODE_AUTO}}));
  // TODO(b/31041577): Handle AWB state machine correctly.
  components.insert(FixedState<uint8_t>(ANDROID_CONTROL_AWB_STATE,
                                        ANDROID_CONTROL_AWB_STATE_CONVERGED));
  // TODO(b/31022153): 3A locks.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_CONTROL_AE_LOCK_AVAILABLE,
                            ANDROID_CONTROL_AE_LOCK_AVAILABLE_TRUE)));
  components.insert(
      NoEffectMenuControl<uint8_t>(ANDROID_CONTROL_AE_LOCK,
                                   DO_NOT_REPORT_OPTIONS,
                                   {ANDROID_CONTROL_AE_LOCK_OFF,
                                    ANDROID_CONTROL_AE_LOCK_ON}));
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_CONTROL_AWB_LOCK_AVAILABLE,
                            ANDROID_CONTROL_AWB_LOCK_AVAILABLE_TRUE)));
  components.insert(
      NoEffectMenuControl<uint8_t>(ANDROID_CONTROL_AWB_LOCK,
                                   DO_NOT_REPORT_OPTIONS,
                                   {ANDROID_CONTROL_AWB_LOCK_OFF,
                                    ANDROID_CONTROL_AWB_LOCK_ON}));
  // TODO(b/30510395): subcomponents of scene modes
  // (may itself be a subcomponent of 3A).
  // Modes from each API that don't match up:
  // Android: FACE_PRIORITY, ACTION, NIGHT_PORTRAIT, THEATRE, STEADYPHOTO,
  // BARCODE, HIGH_SPEED_VIDEO.
  // V4L2: BACKLIGHT, DAWN_DUSK, FALL_COLORS, TEXT.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_SCENE_MODE, ANDROID_CONTROL_AVAILABLE_SCENE_MODES,
      {ANDROID_CONTROL_SCENE_MODE_DISABLED,
       ANDROID_CONTROL_SCENE_MODE_FACE_PRIORITY,
       ANDROID_CONTROL_SCENE_MODE_ACTION, ANDROID_CONTROL_SCENE_MODE_PORTRAIT,
       ANDROID_CONTROL_SCENE_MODE_LANDSCAPE, ANDROID_CONTROL_SCENE_MODE_NIGHT,
       ANDROID_CONTROL_SCENE_MODE_NIGHT_PORTRAIT,
       ANDROID_CONTROL_SCENE_MODE_THEATRE, ANDROID_CONTROL_SCENE_MODE_BEACH,
       ANDROID_CONTROL_SCENE_MODE_SNOW, ANDROID_CONTROL_SCENE_MODE_SUNSET,
       ANDROID_CONTROL_SCENE_MODE_STEADYPHOTO,
       ANDROID_CONTROL_SCENE_MODE_FIREWORKS, ANDROID_CONTROL_SCENE_MODE_SPORTS,
       ANDROID_CONTROL_SCENE_MODE_PARTY,
       ANDROID_CONTROL_SCENE_MODE_CANDLELIGHT},
      {{OTHER_TEMPLATES, ANDROID_CONTROL_SCENE_MODE_DISABLED}}));
  // TODO(b/31022612): Scene mode overrides.
  // Modes from each API that don't match up:
  // Android: POSTERIZE, WHITEBOARD, BLACKBOARD.
  // V4L2: ANTIQUE, ART_FREEZE, EMBOSS, GRASS_GREEN, SKETCH, SKIN_WHITEN,
  // SKY_BLUE, SILHOUETTE, VIVID, SET_CBCR.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_EFFECT_MODE, ANDROID_CONTROL_AVAILABLE_EFFECTS,
      {ANDROID_CONTROL_EFFECT_MODE_OFF, ANDROID_CONTROL_EFFECT_MODE_MONO,
       ANDROID_CONTROL_EFFECT_MODE_NEGATIVE,
       ANDROID_CONTROL_EFFECT_MODE_SOLARIZE, ANDROID_CONTROL_EFFECT_MODE_SEPIA,
       ANDROID_CONTROL_EFFECT_MODE_AQUA},
      {{OTHER_TEMPLATES, ANDROID_CONTROL_EFFECT_MODE_OFF}}));
  // TODO(b/31021654): This should behave as a top level switch, not no effect.
  // Should enforce being set to USE_SCENE_MODE when a scene mode is requested.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_MODE,
      ANDROID_CONTROL_AVAILABLE_MODES,
      {ANDROID_CONTROL_MODE_AUTO, ANDROID_CONTROL_MODE_USE_SCENE_MODE}));

  // Not sure if V4L2 does or doesn't do this, but HAL documentation says
  // all devices must support FAST, and FAST can be equivalent to OFF, so
  // either way it's fine to list. And if FAST is included, HIGH_QUALITY
  // is supposed to be included as well.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_EDGE_MODE,
      ANDROID_EDGE_AVAILABLE_EDGE_MODES,
      {ANDROID_EDGE_MODE_FAST, ANDROID_EDGE_MODE_HIGH_QUALITY},
      {{CAMERA3_TEMPLATE_STILL_CAPTURE, ANDROID_EDGE_MODE_HIGH_QUALITY},
       {OTHER_TEMPLATES, ANDROID_EDGE_MODE_FAST}}));

  // TODO(b/31023454): subcomponents of flash.
  components.insert(
      std::unique_ptr<PartialMetadataInterface>(new Property<uint8_t>(
          ANDROID_FLASH_INFO_AVAILABLE, ANDROID_FLASH_INFO_AVAILABLE_FALSE)));
  components.insert(FixedState<uint8_t>(ANDROID_FLASH_STATE,
                                        ANDROID_FLASH_STATE_UNAVAILABLE));
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_FLASH_MODE, DO_NOT_REPORT_OPTIONS, {ANDROID_FLASH_MODE_OFF}));

  // TODO(30510395): subcomponents of hotpixel.
  // No known V4L2 hot pixel correction. But it might be happening,
  // so we report FAST/HIGH_QUALITY.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_HOT_PIXEL_MODE,
      ANDROID_HOT_PIXEL_AVAILABLE_HOT_PIXEL_MODES,
      {ANDROID_HOT_PIXEL_MODE_FAST, ANDROID_HOT_PIXEL_MODE_HIGH_QUALITY}));
  // ON only needs to be supported for RAW capable devices.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_STATISTICS_HOT_PIXEL_MAP_MODE,
      ANDROID_STATISTICS_INFO_AVAILABLE_HOT_PIXEL_MAP_MODES,
      {ANDROID_STATISTICS_HOT_PIXEL_MAP_MODE_OFF}));

  // TODO(30510395): subcomponents focus/lens.
  // No way to actually get the aperture and focal length
  // in V4L2, but they're required keys, so fake them.
  // Reference hardware: Redmi K50 (2022).  Back: 48MP IMX582, f/1.8, 26mm
  // equivalent.  Front: 20MP, f/2.45.  Reporting the real phone's numbers
  // keeps exposure/exif/FOV sane instead of "Raspberry Pi camera v2".
  const bool k50_back = (id == 0);
  components.insert(
      NoEffectMenuControl<float>(ANDROID_LENS_APERTURE,
                                 ANDROID_LENS_INFO_AVAILABLE_APERTURES,
                                 {k50_back ? 1.8f : 2.45f}));
  // Always assume external-facing.
  components.insert(
      std::unique_ptr<PartialMetadataInterface>(new Property<uint8_t>(
          ANDROID_LENS_FACING,
                                            id == 0 ? ANDROID_LENS_FACING_BACK
                                                    : ANDROID_LENS_FACING_FRONT)));
  components.insert(
      NoEffectMenuControl<float>(ANDROID_LENS_FOCAL_LENGTH,
                                 ANDROID_LENS_INFO_AVAILABLE_FOCAL_LENGTHS,
                                 {k50_back ? 4.74f : 3.30f}));  // K50 26mm/23mm equiv.
  // No known way to get filter densities from V4L2,
  // report 0 to indicate this control is not supported.
  components.insert(
      NoEffectMenuControl<float>(ANDROID_LENS_FILTER_DENSITY,
                                 ANDROID_LENS_INFO_AVAILABLE_FILTER_DENSITIES,
                                 {0.0}));
  // V4L2 focal units do not correspond to a particular physical unit.
  // Back camera is the only one with a real focusing lens, so it reports
  // APPROXIMATE calibration (needed for Camera1 to advertise "infinity");
  // the front camera stays uncalibrated/fixed-focus like a real selfie cam.
  components.insert(
      std::unique_ptr<PartialMetadataInterface>(new Property<uint8_t>(
          ANDROID_LENS_INFO_FOCUS_DISTANCE_CALIBRATION,
          k50_back ? ANDROID_LENS_INFO_FOCUS_DISTANCE_CALIBRATION_APPROXIMATE
                   : ANDROID_LENS_INFO_FOCUS_DISTANCE_CALIBRATION_UNCALIBRATED)));
  // TODO(b/31022711): Focus distance component.
  // Using a NoEffectMenuControl for now because for
  // fixed-focus it meets expectations. Framework may allow
  // setting any value and expect it to be clamped to 0, in which
  // case this will have unexpected behavior (failing on non-0 settings).
  // 10 diopters = 10cm: a self-consistent focusing lens for the back camera.
  // Camera1 keys off MINIMUM_FOCUS_DISTANCE[0] != 0 to enable AF modes at all,
  // so the front camera must stay 0 (fixed focus). The tag itself allows only a
  // single value, so it is a plain Property and the control accepts any focus
  // distance (0 = infinity ... 10 = closest).
  components.insert(NoEffectOptionlessControl<float>(
      ANDROID_LENS_FOCUS_DISTANCE, 0.0f));
  components.insert(std::make_unique<Property<float>>(
      ANDROID_LENS_INFO_MINIMUM_FOCUS_DISTANCE, k50_back ? 10.0f : 0.0f));
  // Hypefocal distance doesn't mean much for a fixed-focus uncalibrated device.
  components.insert(std::make_unique<Property<float>>(
      ANDROID_LENS_INFO_HYPERFOCAL_DISTANCE, 0));

  // No way to know when the lens is moving or not in V4L2.
  components.insert(
      FixedState<uint8_t>(ANDROID_LENS_STATE, ANDROID_LENS_STATE_STATIONARY));
  // No known V4L2 lens shading. But it might be happening,
  // so report FAST/HIGH_QUALITY.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_SHADING_MODE,
      ANDROID_SHADING_AVAILABLE_MODES,
      {ANDROID_SHADING_MODE_FAST, ANDROID_SHADING_MODE_HIGH_QUALITY}));
  // ON only needs to be supported for RAW capable devices.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_STATISTICS_LENS_SHADING_MAP_MODE,
      ANDROID_STATISTICS_INFO_AVAILABLE_LENS_SHADING_MAP_MODES,
      {ANDROID_STATISTICS_LENS_SHADING_MAP_MODE_OFF}));
  // V4L2 doesn't differentiate between OPTICAL and VIDEO stabilization,
  // so only report one (and report the other as OFF).
  components.insert(V4L2ControlOrDefault<uint8_t>(
      ControlType::kMenu,
      ANDROID_CONTROL_VIDEO_STABILIZATION_MODE,
      ANDROID_CONTROL_AVAILABLE_VIDEO_STABILIZATION_MODES,
      device,
      V4L2_CID_IMAGE_STABILIZATION,
      std::shared_ptr<ConverterInterface<uint8_t, int32_t>>(new EnumConverter(
          {{0, ANDROID_CONTROL_VIDEO_STABILIZATION_MODE_OFF},
           {1, ANDROID_CONTROL_VIDEO_STABILIZATION_MODE_ON}})),
      ANDROID_CONTROL_VIDEO_STABILIZATION_MODE_OFF));
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_LENS_OPTICAL_STABILIZATION_MODE,
      ANDROID_LENS_INFO_AVAILABLE_OPTICAL_STABILIZATION,
      {ANDROID_LENS_OPTICAL_STABILIZATION_MODE_OFF}));
  // TODO(b/31017806): This should definitely have a different default depending
  // on template.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_CONTROL_CAPTURE_INTENT,
      DO_NOT_REPORT_OPTIONS,
      {ANDROID_CONTROL_CAPTURE_INTENT_CUSTOM,
       ANDROID_CONTROL_CAPTURE_INTENT_PREVIEW,
       ANDROID_CONTROL_CAPTURE_INTENT_STILL_CAPTURE,
       ANDROID_CONTROL_CAPTURE_INTENT_VIDEO_RECORD,
       ANDROID_CONTROL_CAPTURE_INTENT_VIDEO_SNAPSHOT,
       ANDROID_CONTROL_CAPTURE_INTENT_ZERO_SHUTTER_LAG,
       ANDROID_CONTROL_CAPTURE_INTENT_MANUAL},
      {{CAMERA3_TEMPLATE_PREVIEW, ANDROID_CONTROL_CAPTURE_INTENT_PREVIEW},
       {CAMERA3_TEMPLATE_STILL_CAPTURE,
        ANDROID_CONTROL_CAPTURE_INTENT_STILL_CAPTURE},
       {CAMERA3_TEMPLATE_VIDEO_RECORD,
        ANDROID_CONTROL_CAPTURE_INTENT_VIDEO_RECORD},
       {CAMERA3_TEMPLATE_VIDEO_SNAPSHOT,
        ANDROID_CONTROL_CAPTURE_INTENT_VIDEO_SNAPSHOT},
       {CAMERA3_TEMPLATE_ZERO_SHUTTER_LAG,
        ANDROID_CONTROL_CAPTURE_INTENT_ZERO_SHUTTER_LAG},
       {CAMERA3_TEMPLATE_MANUAL, ANDROID_CONTROL_CAPTURE_INTENT_MANUAL},
       {OTHER_TEMPLATES, ANDROID_CONTROL_CAPTURE_INTENT_CUSTOM}}));

  // Unable to control noise reduction in V4L2 devices,
  // but FAST is allowed to be the same as OFF,
  // and HIGH_QUALITY can be the same as FAST.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_NOISE_REDUCTION_MODE,
      ANDROID_NOISE_REDUCTION_AVAILABLE_NOISE_REDUCTION_MODES,
      {ANDROID_NOISE_REDUCTION_MODE_FAST,
       ANDROID_NOISE_REDUCTION_MODE_HIGH_QUALITY},
      {{CAMERA3_TEMPLATE_STILL_CAPTURE,
        ANDROID_NOISE_REDUCTION_MODE_HIGH_QUALITY},
       {OTHER_TEMPLATES, ANDROID_NOISE_REDUCTION_MODE_FAST}}));

  // TODO(30510395): subcomponents of formats/streams.
  // For now, no thumbnails available (only [0,0], the "no thumbnail" size).
  // TODO(b/29580107): Could end up with a mismatch between request & result,
  // since V4L2 doesn't actually allow for thumbnail size control.
  components.insert(NoEffectMenuControl<std::array<int32_t, 2>>(
      ANDROID_JPEG_THUMBNAIL_SIZE,
      ANDROID_JPEG_AVAILABLE_THUMBNAIL_SIZES,
      {{{0, 0}}, {{160, 120}}, {{320, 240}}},
      {{OTHER_TEMPLATES, {{0, 0}}}}));
  // TODO(b/31022752): Get this from the device, not constant.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<int32_t>(ANDROID_JPEG_MAX_SIZE, kV4L2MaxJpegSize)));
  // TODO(b/31021672): Other JPEG controls (GPS, quality, orientation).
  // TODO(b/29939583): V4L2 can only support 1 stream at a time.
  // For now, just reporting minimum allowable for LIMITED devices.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::array<int32_t, 3>>(
          ANDROID_REQUEST_MAX_NUM_OUTPUT_STREAMS,
          {{/* Raw */ 0, /* Non-stalling */ 2, /* Stalling */ 1}})));
  // Reprocessing not supported.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<int32_t>(ANDROID_REQUEST_MAX_NUM_INPUT_STREAMS, 0)));
  // No way to know pipeline depth for V4L2, so fake with max allowable latency.
  // Doesn't mean much without per-frame controls anyways.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_REQUEST_PIPELINE_MAX_DEPTH, 4)));
  components.insert(FixedState<uint8_t>(ANDROID_REQUEST_PIPELINE_DEPTH, 4));
  // "LIMITED devices are strongly encouraged to use a non-negative value.
  // If UNKNOWN is used here then app developers do not have a way to know
  // when sensor settings have been applied." - Unfortunately, V4L2 doesn't
  // really help here either. Could even be that adjusting settings mid-stream
  // blocks in V4L2, and should be avoided.
  components.insert(
      std::unique_ptr<PartialMetadataInterface>(new Property<int32_t>(
          ANDROID_SYNC_MAX_LATENCY, ANDROID_SYNC_MAX_LATENCY_UNKNOWN)));
  // Never know when controls are synced.
  components.insert(FixedState<int64_t>(ANDROID_SYNC_FRAME_NUMBER,
                                        ANDROID_SYNC_FRAME_NUMBER_UNKNOWN));

  // TODO(b/31022480): subcomponents of cropping/sensors.
  // Need ANDROID_SCALER_CROP_REGION control support.
  // V4L2 VIDIOC_CROPCAP doesn't give a way to query this;
  // it's driver dependent. For now, assume freeform, and
  // some cameras may just behave badly.
  // TODO(b/29579652): Figure out a way to determine this.
  // Real phones advertise digital zoom; the crop region is a no-op here, but
  // reporting 1.0 makes legacy clients disable zoom entirely.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<float>(ANDROID_SCALER_AVAILABLE_MAX_DIGITAL_ZOOM, 8.0f)));
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_SCALER_CROPPING_TYPE,
                            ANDROID_SCALER_CROPPING_TYPE_FREEFORM)));
  // Spoof pixel array size for now, eventually get from CROPCAP.
  // K50: 48MP back (8000x6000), 20MP front (5184x3888) - 4:3 like the node.
  std::array<int32_t, 2> pixel_array_size =
      k50_back ? std::array<int32_t, 2>{{8000, 6000}}
               : std::array<int32_t, 2>{{5184, 3888}};
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::array<int32_t, 2>>(ANDROID_SENSOR_INFO_PIXEL_ARRAY_SIZE,
                                           pixel_array_size)));
  // Active array size is {x-offset, y-offset, width, height}, relative to
  // the pixel array size, with {0, 0} being the top left. Since there's no way
  // to get this in V4L2, assume the full pixel array is the active array.
  std::array<int32_t, 4> active_array_size = {
      {0, 0, pixel_array_size[0], pixel_array_size[1]}};
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::array<int32_t, 4>>(
          ANDROID_SENSOR_INFO_ACTIVE_ARRAY_SIZE, active_array_size)));
  // Real K50 sensors are Bayer RGGB; without this tag some camera stacks treat
  // the device as monochrome / unknown and skip or mis-handle color pipelines.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_SENSOR_INFO_COLOR_FILTER_ARRANGEMENT,
                            ANDROID_SENSOR_INFO_COLOR_FILTER_ARRANGEMENT_RGGB)));
  // This is really more freeform than a menu control, but since we're
  // restricting it to not being used anyways this works for now.
  components.insert(NoEffectMenuControl<std::array<int32_t, 4>>(
      ANDROID_SCALER_CROP_REGION, DO_NOT_REPORT_OPTIONS, {active_array_size}));
  // No way to get in V4L2, so faked. RPi camera v2 is 3.674 x 2.760 mm.
  // Physical size is used in framework calculations (field of view,
  // pixel pitch, etc.), so faking it may have unexpected results.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::array<float, 2>>(ANDROID_SENSOR_INFO_PHYSICAL_SIZE,
                                         {{k50_back ? 6.40f : 4.60f,
                                           k50_back ? 4.80f : 3.45f}})));
  // HAL uses BOOTTIME timestamps.
  // TODO(b/29457051): make sure timestamps are consistent throughout the HAL.
  // Real devices report REALTIME; timestamps come from CLOCK_BOOTTIME, which
  // matches the REALTIME source on devices that never suspend.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_SENSOR_INFO_TIMESTAMP_SOURCE,
                            ANDROID_SENSOR_INFO_TIMESTAMP_SOURCE_REALTIME)));
  components.insert(std::make_unique<State<int64_t>>(
      ANDROID_SENSOR_TIMESTAMP, std::make_unique<BoottimeStateDelegate>()));
  // Estimated from the 30fps stream period: a real rolling shutter exposes
  // rows over most of the frame interval, so report ~2/3 of it instead of 0.
  components.insert(FixedState<int64_t>(
      ANDROID_SENSOR_ROLLING_SHUTTER_SKEW, k50_back ? 22000000 : 18000000));
  // No way to actually get orientation from V4L2.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<int32_t>(ANDROID_SENSOR_ORIENTATION, 0)));
  // TODO(b/31023611): Sensor frame duration. Range should
  // be dependent on the stream configuration being used.
  // No test patterns supported.
  components.insert(
      NoEffectMenuControl<int32_t>(ANDROID_SENSOR_TEST_PATTERN_MODE,
                                   ANDROID_SENSOR_AVAILABLE_TEST_PATTERN_MODES,
                                   {ANDROID_SENSOR_TEST_PATTERN_MODE_OFF}));

  // TODO(b/30510395): subcomponents of face detection.
  // Face detection not supported.
  components.insert(NoEffectMenuControl<uint8_t>(
      ANDROID_STATISTICS_FACE_DETECT_MODE,
      ANDROID_STATISTICS_INFO_AVAILABLE_FACE_DETECT_MODES,
      {ANDROID_STATISTICS_FACE_DETECT_MODE_OFF}));
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<int32_t>(ANDROID_STATISTICS_INFO_MAX_FACE_COUNT, 0)));

  // No way to get detected scene flicker from V4L2.
  components.insert(FixedState<uint8_t>(ANDROID_STATISTICS_SCENE_FLICKER,
                                        ANDROID_STATISTICS_SCENE_FLICKER_NONE));

  // TOOD(b/31023265): V4L2_CID_FLASH_INDICATOR_INTENSITY could be queried
  // to see if there's a transmit LED. Would need to translate HAL off/on
  // enum to slider min/max value. For now, no LEDs available.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_LED_AVAILABLE_LEDS, {})));

  /* Capabilities. */
  // The V4L2Metadata pretends to at least meet the
  // "LIMITED" and "BACKWARD_COMPATIBLE" functionality requirements.
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<uint8_t>(ANDROID_INFO_SUPPORTED_HARDWARE_LEVEL,
                            ANDROID_INFO_SUPPORTED_HARDWARE_LEVEL_LIMITED)));
  components.insert(std::unique_ptr<PartialMetadataInterface>(
      new Property<std::vector<uint8_t>>(
          ANDROID_REQUEST_AVAILABLE_CAPABILITIES,
          {ANDROID_REQUEST_AVAILABLE_CAPABILITIES_BACKWARD_COMPATIBLE})));

  // Request is unused, and can be any value,
  // but that value needs to be propagated.
  components.insert(NoEffectOptionlessControl<int32_t>(ANDROID_REQUEST_ID, 0));

  // Metadata is returned in a single result; not multiple pieces.
  components.insert(std::make_unique<Property<int32_t>>(
      ANDROID_REQUEST_PARTIAL_RESULT_COUNT, 1));

  int res =
      AddFormatComponents(device, std::inserter(components, components.end()));
  if (res) {
    HAL_LOGE("Failed to initialize format components.");
    return res;
  }

  *result = std::make_unique<Metadata>(std::move(components));
  return 0;
}

}  // namespace v4l2_camera_hal
