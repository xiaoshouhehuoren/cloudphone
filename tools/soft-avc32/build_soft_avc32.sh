#!/bin/bash
# g98 checkout root = cloudphone/tools/soft-avc32/../../.. ; override with G98_ROOT.
cd "${G98_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
export TMPDIR="C:/Users/rosetta/Desktop/g98/tmpb" TMP="$TMPDIR" TEMP="$TMPDIR"
mkdir -p tmpb build/avc32
BIN=android-ndk-r26d/toolchains/llvm/prebuilt/windows-x86_64/bin
CC=$BIN/armv7a-linux-androideabi33-clang
CXX=$BIN/armv7a-linux-androideabi33-clang++
A=aosp/external_libavc13
V=aosp/a13_platform_frameworks_av
N=aosp/a13_platform_frameworks_native
C=aosp/a13_platform_system_core
B=aosp/a13_platform_system_libbase
G=aosp/a13_platform_system_logging
LC=aosp/a13_external_libcxx
INC="-I$V/media/libstagefright/codecs/avcenc -I$V/media/libstagefright/omx/include -I$V/media/libstagefright/include -I$V/media/libstagefright/foundation/include -I$V/include -I$N/headers/media_plugin -I$N/headers/media_plugin/media/openmax -I$N/headers/media_plugin/media/hardware -I$N/libs/nativewindow/include -I$N/libs/utils/include -I$C/libcutils/include -I$C/libutils/include -I$C/libsystem/include -I$C/libsync/include -I$G/liblog/include -I$B/include -I$A/common -I$A/encoder -I$A/encoder/arm -I$A/common/arm"
CXXFLAGS="-std=gnu++17 -fno-rtti -fno-exceptions -fPIC -O2 -march=armv7-a -mfpu=neon -mfloat-abi=softfp -nostdinc++ -I$LC/include -include $B/include/android-base/macros.h $INC"
echo '=== compile SoftAVCEnc.cpp ==='
$CXX $CXXFLAGS -c $V/media/libstagefright/codecs/avcenc/SoftAVCEnc.cpp -o build/avc32/SoftAVCEnc.o 2>&1 | head -40
[ -f build/avc32/SoftAVCEnc.o ] || { echo CPP_FAILED; exit 1; }
echo CPP_OK
CFLAGS="-fPIC -O3 -DNDEBUG -UHP_PL -DN_MB_ENABLE -DARM -march=armv7-a -mfpu=neon -mfloat-abi=softfp -I$A/common -I$A/encoder -I$A/encoder/arm -I$A/common/arm -I$G/liblog/include -I$C/libcutils/include"
SRCS="common/ih264_resi_trans_quant.c common/ih264_iquant_itrans_recon.c common/ih264_ihadamard_scaling.c common/ih264_inter_pred_filters.c common/ih264_luma_intra_pred_filters.c common/ih264_chroma_intra_pred_filters.c common/ih264_padding.c common/ih264_mem_fns.c common/ih264_deblk_edge_filters.c common/ih264_deblk_tables.c common/ih264_cavlc_tables.c common/ih264_cabac_tables.c common/ih264_common_tables.c common/ih264_trans_data.c common/ih264_buf_mgr.c common/ih264_dpb_mgr.c common/ih264_list.c common/ithread.c encoder/ih264e_globals.c encoder/ih264e_intra_modes_eval.c encoder/ih264e_half_pel.c encoder/ih264e_mc.c encoder/ih264e_me.c encoder/ih264e_rc_mem_interface.c encoder/ih264e_time_stamp.c encoder/ih264e_modify_frm_rate.c encoder/ih264e_rate_control.c encoder/ih264e_core_coding.c encoder/ih264e_deblk.c encoder/ih264e_api.c encoder/ih264e_process.c encoder/ih264e_encode.c encoder/ih264e_utils.c encoder/ih264e_version.c encoder/ih264e_bitstream.c encoder/ih264e_cavlc.c encoder/ih264e_cabac_init.c encoder/ih264e_cabac.c encoder/ih264e_cabac_encode.c encoder/ih264e_encode_header.c encoder/ih264e_function_selector_generic.c encoder/ih264e_fmt_conv.c encoder/irc_rate_control_api.c encoder/irc_bit_allocation.c encoder/irc_cbr_buffer_control.c encoder/irc_est_sad.c encoder/irc_fixed_point_error_bits.c encoder/irc_frame_info_collector.c encoder/irc_mb_model_based.c encoder/irc_picture_type.c encoder/irc_rd_model.c encoder/irc_vbr_storage_vbv.c encoder/irc_vbr_str_prms.c encoder/ime.c encoder/ime_distortion_metrics.c encoder/ih264e_sei.c encoder/arm/ih264e_function_selector.c encoder/arm/ih264e_function_selector_a9q.c"
ASMS="common/arm/ih264_arm_memory_barrier.s common/arm/ih264_resi_trans_quant_a9.s common/arm/ih264_iquant_itrans_recon_a9.s common/arm/ih264_iquant_itrans_recon_dc_a9.s common/arm/ih264_ihadamard_scaling_a9.s common/arm/ih264_deblk_chroma_a9.s common/arm/ih264_deblk_luma_a9.s common/arm/ih264_intra_pred_chroma_a9q.s common/arm/ih264_intra_pred_luma_16x16_a9q.s common/arm/ih264_intra_pred_luma_4x4_a9q.s common/arm/ih264_intra_pred_luma_8x8_a9q.s common/arm/ih264_inter_pred_chroma_a9q.s common/arm/ih264_inter_pred_filters_luma_horz_a9q.s common/arm/ih264_inter_pred_filters_luma_vert_a9q.s common/arm/ih264_inter_pred_luma_bilinear_a9q.s common/arm/ih264_inter_pred_luma_copy_a9q.s common/arm/ih264_padding_neon.s common/arm/ih264_mem_fns_neon.s encoder/arm/ih264e_evaluate_intra16x16_modes_a9q.s encoder/arm/ih264e_evaluate_intra4x4_modes_a9q.s encoder/arm/ih264e_evaluate_intra_chroma_modes_a9q.s encoder/arm/ih264e_half_pel.s encoder/arm/ih264e_fmt_conv.s encoder/arm/ime_distortion_metrics_a9q.s"
OBJS=""
FAIL=0
for S in $SRCS $ASMS; do
  O=build/avc32/$(echo "$S" | tr '/.' '__').o
  OBJS="$OBJS $O"
  [ -f "$O" ] && continue
  $CC $CFLAGS -c "$A/$S" -o "$O" > "$O.log" 2>&1 || { echo "FAIL $S"; head -12 "$O.log"; FAIL=1; break; }
done
[ "$FAIL" = "0" ] || exit 1
echo '=== link ==='
$CXX -shared -Wl,-soname,libstagefright_soft_avcenc.so -o libstagefright_soft_avcenc.so build/avc32/SoftAVCEnc.o $OBJS -Llinklibs32 -lstagefright_softomx -lstagefright_foundation -llog -lutils -lcutils -lc++ -lbase -ldl -lm 2>&1 | head -30
ls -l libstagefright_soft_avcenc.so || { echo LINK_FAILED; exit 1; }
$BIN/llvm-readelf.exe -d libstagefright_soft_avcenc.so | grep NEEDED
$BIN/llvm-nm.exe -D libstagefright_soft_avcenc.so | grep -c createSoftOMXComponent
echo BUILD_DONE
