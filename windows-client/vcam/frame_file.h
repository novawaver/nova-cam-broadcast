// Layout of the file shared by the client (writer) and the camera (reader).
//
// A plain file rather than a named memory section: the camera runs inside the
// Frame Server service, in another session, and creating a Global\ section
// needs a privilege that normal desktop apps do not have. Mapping one file from
// both sides needs only a file ACL.
//
// Keep in sync with windows-client/frame_writer.py.
#pragma once
#include <cstdint>

constexpr uint32_t kFrameMagic = 0x4242564E;  // "NVBB"
constexpr uint32_t kFrameVersion = 1;
constexpr uint32_t kWidth = 1280;
constexpr uint32_t kHeight = 720;
constexpr uint32_t kFps = 30;
constexpr uint32_t kFrameBytes = kWidth * kHeight * 3 / 2;  // NV12
// The writer cycles through the slots, so a reader copying the newest one is
// not overwritten until two whole frame times have passed.
constexpr uint32_t kSlots = 3;
constexpr uint32_t kHeaderBytes = 4096;
// With no new frame for this long, the camera shows black instead of freezing.
constexpr uint64_t kStaleMs = 2000;

#pragma pack(push, 1)
struct FrameHeader {
    uint32_t magic;
    uint32_t version;
    uint32_t width;
    uint32_t height;
    uint32_t latestSlot;
    uint32_t reserved;
    uint64_t sequence;
    uint64_t updatedTickMs;  // GetTickCount64 at the last write; system-wide
};
#pragma pack(pop)
