// Virtual camera media source, loaded by the Windows Frame Server service.
//
// Serves 1280x720 NV12 at 30 FPS from the shared frame file. Frame Server
// creates this COM class, asks it for an IMFMediaSource, and shares the stream
// with every app that opens the camera.
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mferror.h>
#include <mfvirtualcamera.h>
#include <ks.h>
#include <ksproxy.h>
#include <wrl/client.h>
#include <wrl/implements.h>

#include <atomic>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>

#include "frame_file.h"

using Microsoft::WRL::ChainInterfaces;
using Microsoft::WRL::ClassicCom;
using Microsoft::WRL::ComPtr;
using Microsoft::WRL::Make;
using Microsoft::WRL::RuntimeClass;
using Microsoft::WRL::RuntimeClassFlags;

// {7B3E2C1A-5D4F-4E8B-9A61-2F6C8D1E4A90}
static const CLSID CLSID_NvbbCamera = {
    0x7b3e2c1a, 0x5d4f, 0x4e8b, {0x9a, 0x61, 0x2f, 0x6c, 0x8d, 0x1e, 0x4a, 0x90}};
static const wchar_t kClsidString[] = L"{7B3E2C1A-5D4F-4E8B-9A61-2F6C8D1E4A90}";
// PINNAME_VIDEO_CAPTURE
static const GUID kPinVideoCapture = {
    0xfb6c4281, 0x0353, 0x11d1, {0x90, 0x5f, 0x00, 0x00, 0xc0, 0xcc, 0x16, 0xba}};

static HMODULE g_module = nullptr;
static std::atomic<long> g_objects{0};

#define RETURN_IF_FAILED(expr)        \
    do {                              \
        const HRESULT _hr = (expr);   \
        if (FAILED(_hr)) return _hr;  \
    } while (0)

// Debug builds (/DNVBB_TRACE) log activation and every interface requested.
#ifdef NVBB_TRACE
#include <cstdio>
static void Trace(const char* where, const GUID* iid, HRESULT hr) {
    wchar_t data[MAX_PATH];
    if (!GetEnvironmentVariableW(L"ProgramData", data, MAX_PATH)) return;
    const std::wstring path = std::wstring(data) + L"\\NvidiaBroadcastBridge\\vcam.log";
    FILE* file = _wfopen(path.c_str(), L"a");
    if (!file) return;
    wchar_t text[64] = L"-";
    if (iid) StringFromGUID2(*iid, text, 64);
    fprintf(file, "%llu pid=%lu %s %ls hr=0x%08lX\n", GetTickCount64(), GetCurrentProcessId(), where,
            text, static_cast<unsigned long>(hr));
    fclose(file);
}
#define TRACE_QI(Name)                                                 \
    STDMETHODIMP QueryInterface(REFIID riid, void** result) override { \
        const HRESULT hr = RuntimeClass::QueryInterface(riid, result); \
        Trace(Name "::QI", &riid, hr);                                 \
        return hr;                                                     \
    }
#else
#define Trace(where, iid, hr) ((void)0)
#define TRACE_QI(Name)
#endif

// --- shared frame file --------------------------------------------------------

class FrameReader {
public:
    ~FrameReader() { Close(); }

    // Copies the newest frame. Returns false if none is live, so the caller
    // shows black: no client running, client stopped, or stream interrupted.
    bool Read(BYTE* destination) {
        if (!view_ && !Open()) return false;
        auto* header = static_cast<const volatile FrameHeader*>(view_);
        if (header->magic != kFrameMagic || header->version != kFrameVersion ||
            header->sequence == 0 || header->latestSlot >= kSlots) {
            return false;
        }
        if (GetTickCount64() - header->updatedTickMs > kStaleMs) return false;
        const auto* base = static_cast<const BYTE*>(view_) + kHeaderBytes;
        memcpy(destination, base + header->latestSlot * size_t(kFrameBytes), kFrameBytes);
        return true;
    }

private:
    bool Open() {
        const ULONGLONG now = GetTickCount64();
        if (now - lastAttempt_ < 1000 && lastAttempt_ != 0) return false;
        lastAttempt_ = now;

        wchar_t data[MAX_PATH];
        if (!GetEnvironmentVariableW(L"ProgramData", data, MAX_PATH)) return false;
        const std::wstring path = std::wstring(data) + L"\\NvidiaBroadcastBridge\\camera.frames";
        const SIZE_T size = kHeaderBytes + SIZE_T(kFrameBytes) * kSlots;

        file_ = CreateFileW(path.c_str(), GENERIC_READ,
                            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                            nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        if (file_ == INVALID_HANDLE_VALUE) return false;
        LARGE_INTEGER actual{};
        if (!GetFileSizeEx(file_, &actual) || SIZE_T(actual.QuadPart) < size) {
            Close();
            return false;
        }
        mapping_ = CreateFileMappingW(file_, nullptr, PAGE_READONLY, 0, 0, nullptr);
        if (mapping_) view_ = MapViewOfFile(mapping_, FILE_MAP_READ, 0, 0, size);
        if (!view_) {
            Close();
            return false;
        }
        return true;
    }

    void Close() {
        if (view_) UnmapViewOfFile(view_);
        if (mapping_) CloseHandle(mapping_);
        if (file_ != INVALID_HANDLE_VALUE) CloseHandle(file_);
        view_ = nullptr;
        mapping_ = nullptr;
        file_ = INVALID_HANDLE_VALUE;
    }

    HANDLE file_ = INVALID_HANDLE_VALUE;
    HANDLE mapping_ = nullptr;
    void* view_ = nullptr;
    ULONGLONG lastAttempt_ = 0;
};

#define FORWARD_ATTRIBUTES(store)                                                               \
    STDMETHODIMP GetItem(REFGUID k, PROPVARIANT* v) override { return store->GetItem(k, v); }   \
    STDMETHODIMP GetItemType(REFGUID k, MF_ATTRIBUTE_TYPE* t) override {                        \
        return store->GetItemType(k, t);                                                        \
    }                                                                                           \
    STDMETHODIMP CompareItem(REFGUID k, REFPROPVARIANT v, BOOL* r) override {                   \
        return store->CompareItem(k, v, r);                                                     \
    }                                                                                           \
    STDMETHODIMP Compare(IMFAttributes* o, MF_ATTRIBUTES_MATCH_TYPE t, BOOL* r) override {      \
        return store->Compare(o, t, r);                                                         \
    }                                                                                           \
    STDMETHODIMP GetUINT32(REFGUID k, UINT32* v) override { return store->GetUINT32(k, v); }    \
    STDMETHODIMP GetUINT64(REFGUID k, UINT64* v) override { return store->GetUINT64(k, v); }    \
    STDMETHODIMP GetDouble(REFGUID k, double* v) override { return store->GetDouble(k, v); }    \
    STDMETHODIMP GetGUID(REFGUID k, GUID* v) override { return store->GetGUID(k, v); }          \
    STDMETHODIMP GetStringLength(REFGUID k, UINT32* v) override {                               \
        return store->GetStringLength(k, v);                                                    \
    }                                                                                           \
    STDMETHODIMP GetString(REFGUID k, LPWSTR v, UINT32 s, UINT32* l) override {                 \
        return store->GetString(k, v, s, l);                                                    \
    }                                                                                           \
    STDMETHODIMP GetAllocatedString(REFGUID k, LPWSTR* v, UINT32* l) override {                 \
        return store->GetAllocatedString(k, v, l);                                              \
    }                                                                                           \
    STDMETHODIMP GetBlobSize(REFGUID k, UINT32* v) override { return store->GetBlobSize(k, v); }\
    STDMETHODIMP GetBlob(REFGUID k, UINT8* b, UINT32 s, UINT32* r) override {                   \
        return store->GetBlob(k, b, s, r);                                                      \
    }                                                                                           \
    STDMETHODIMP GetAllocatedBlob(REFGUID k, UINT8** b, UINT32* s) override {                   \
        return store->GetAllocatedBlob(k, b, s);                                                \
    }                                                                                           \
    STDMETHODIMP GetUnknown(REFGUID k, REFIID i, LPVOID* v) override {                          \
        return store->GetUnknown(k, i, v);                                                      \
    }                                                                                           \
    STDMETHODIMP SetItem(REFGUID k, REFPROPVARIANT v) override { return store->SetItem(k, v); } \
    STDMETHODIMP DeleteItem(REFGUID k) override { return store->DeleteItem(k); }                \
    STDMETHODIMP DeleteAllItems() override { return store->DeleteAllItems(); }                  \
    STDMETHODIMP SetUINT32(REFGUID k, UINT32 v) override { return store->SetUINT32(k, v); }     \
    STDMETHODIMP SetUINT64(REFGUID k, UINT64 v) override { return store->SetUINT64(k, v); }     \
    STDMETHODIMP SetDouble(REFGUID k, double v) override { return store->SetDouble(k, v); }     \
    STDMETHODIMP SetGUID(REFGUID k, REFGUID v) override { return store->SetGUID(k, v); }        \
    STDMETHODIMP SetString(REFGUID k, LPCWSTR v) override { return store->SetString(k, v); }    \
    STDMETHODIMP SetBlob(REFGUID k, const UINT8* b, UINT32 s) override {                        \
        return store->SetBlob(k, b, s);                                                         \
    }                                                                                           \
    STDMETHODIMP SetUnknown(REFGUID k, IUnknown* v) override { return store->SetUnknown(k, v); }\
    STDMETHODIMP LockStore() override { return store->LockStore(); }                            \
    STDMETHODIMP UnlockStore() override { return store->UnlockStore(); }                        \
    STDMETHODIMP GetCount(UINT32* c) override { return store->GetCount(c); }                    \
    STDMETHODIMP GetItemByIndex(UINT32 i, GUID* k, PROPVARIANT* v) override {                   \
        return store->GetItemByIndex(i, k, v);                                                  \
    }                                                                                           \
    STDMETHODIMP CopyAllItems(IMFAttributes* d) override { return store->CopyAllItems(d); }

// --- stream -------------------------------------------------------------------

class Source;

// ChainInterfaces matters: WRL answers QueryInterface only for interfaces it is
// told about, so base interfaces would otherwise be reported as unsupported.
class Stream : public RuntimeClass<RuntimeClassFlags<ClassicCom>,
                                   ChainInterfaces<IMFMediaStream2, IMFMediaStream, IMFMediaEventGenerator>,
                                   IKsControl> {
public:
    TRACE_QI("Stream")
    Stream() { ++g_objects; }
    ~Stream() override {
        StopWorker();
        --g_objects;
    }

    HRESULT Initialize(IMFMediaSource* source) {
        source_ = source;
        RETURN_IF_FAILED(MFCreateEventQueue(&queue_));

        ComPtr<IMFMediaType> type;
        RETURN_IF_FAILED(MFCreateMediaType(&type));
        type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
        type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
        MFSetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, kWidth, kHeight);
        MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE, kFps, 1);
        MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE_RANGE_MAX, kFps, 1);
        MFSetAttributeRatio(type.Get(), MF_MT_FRAME_RATE_RANGE_MIN, kFps, 1);
        MFSetAttributeRatio(type.Get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
        type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
        type->SetUINT32(MF_MT_ALL_SAMPLES_INDEPENDENT, TRUE);
        type->SetUINT32(MF_MT_FIXED_SIZE_SAMPLES, TRUE);
        type->SetUINT32(MF_MT_SAMPLE_SIZE, kFrameBytes);
        type->SetUINT32(MF_MT_DEFAULT_STRIDE, kWidth);
        type->SetUINT32(MF_MT_AVG_BITRATE, kFrameBytes * 8 * kFps);

        IMFMediaType* types[] = {type.Get()};
        RETURN_IF_FAILED(MFCreateStreamDescriptor(0, 1, types, &descriptor_));
        ComPtr<IMFMediaTypeHandler> handler;
        RETURN_IF_FAILED(descriptor_->GetMediaTypeHandler(&handler));
        RETURN_IF_FAILED(handler->SetCurrentMediaType(type.Get()));

        descriptor_->SetGUID(MF_DEVICESTREAM_STREAM_CATEGORY, kPinVideoCapture);
        descriptor_->SetUINT32(MF_DEVICESTREAM_STREAM_ID, 0);
        descriptor_->SetUINT32(MF_DEVICESTREAM_FRAMESERVER_SHARED, 1);
        descriptor_->SetUINT32(MF_DEVICESTREAM_ATTRIBUTE_FRAMESOURCE_TYPES,
                               MFFrameSourceTypes_Color);
        return S_OK;
    }

    IMFStreamDescriptor* Descriptor() { return descriptor_.Get(); }

    HRESULT Start(const PROPVARIANT* startTime) {
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;
        state_ = MF_STREAM_STATE_RUNNING;
        StartWorker();
        return queue_->QueueEventParamVar(MEStreamStarted, GUID_NULL, S_OK, startTime);
    }

    HRESULT Stop() {
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;
        state_ = MF_STREAM_STATE_STOPPED;
        tokens_.clear();
        return queue_->QueueEventParamVar(MEStreamStopped, GUID_NULL, S_OK, nullptr);
    }

    void Shutdown() {
        StopWorker();
        std::lock_guard lock(mutex_);
        if (queue_) queue_->Shutdown();
        queue_.Reset();
        descriptor_.Reset();
        source_.Reset();
        tokens_.clear();
    }

    // IMFMediaEventGenerator
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* callback, IUnknown* state) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->BeginGetEvent(callback, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* result, IMFMediaEvent** event) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->EndGetEvent(result, event) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** event) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->GetEvent(flags, event) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType type, REFGUID extended, HRESULT status,
                            const PROPVARIANT* value) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->QueueEventParamVar(type, extended, status, value) : MF_E_SHUTDOWN;
    }

    // IMFMediaStream
    STDMETHODIMP GetMediaSource(IMFMediaSource** source) override {
        if (!source) return E_POINTER;
        std::lock_guard lock(mutex_);
        return source_ ? source_.CopyTo(source) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP GetStreamDescriptor(IMFStreamDescriptor** descriptor) override {
        if (!descriptor) return E_POINTER;
        std::lock_guard lock(mutex_);
        return descriptor_ ? descriptor_.CopyTo(descriptor) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP RequestSample(IUnknown* token) override {
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;
        if (state_ != MF_STREAM_STATE_RUNNING) return MF_E_MEDIA_SOURCE_WRONGSTATE;
        tokens_.emplace_back(token);
        return S_OK;
    }

    // IMFMediaStream2
    STDMETHODIMP SetStreamState(MF_STREAM_STATE value) override {
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;
        state_ = value;
        if (value == MF_STREAM_STATE_RUNNING) StartWorker();
        return S_OK;
    }
    STDMETHODIMP GetStreamState(MF_STREAM_STATE* value) override {
        if (!value) return E_POINTER;
        std::lock_guard lock(mutex_);
        *value = state_;
        return S_OK;
    }

    // IKsControl: this camera has no driver-level properties.
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }

private:
    ComPtr<IMFMediaEventQueue> Queue() {
        std::lock_guard lock(mutex_);
        return queue_;
    }

    void StartWorker() {
        if (worker_.joinable()) return;
        running_ = true;
        worker_ = std::thread([this] { Work(); });
    }

    void StopWorker() {
        running_ = false;
        if (worker_.joinable()) worker_.join();
    }

    // Requests are answered at the frame rate, not as they arrive: answering
    // immediately would make a fast consumer spin at full CPU.
    void Work() {
        const ULONGLONG intervalUs = 1000000ULL / kFps;
        LARGE_INTEGER frequency, last{};
        QueryPerformanceFrequency(&frequency);
        while (running_) {
            LARGE_INTEGER now;
            QueryPerformanceCounter(&now);
            const ULONGLONG elapsedUs =
                (now.QuadPart - last.QuadPart) * 1000000ULL / frequency.QuadPart;
            if (elapsedUs < intervalUs) {
                Sleep(1);
                continue;
            }
            ComPtr<IUnknown> token;
            ComPtr<IMFMediaEventQueue> queue;
            bool hasToken = false;
            {
                std::lock_guard lock(mutex_);
                if (state_ == MF_STREAM_STATE_RUNNING && !tokens_.empty() && queue_) {
                    token = tokens_.front();
                    tokens_.pop_front();
                    queue = queue_;
                    hasToken = true;
                }
            }
            if (!hasToken) {
                Sleep(1);
                continue;
            }
            last = now;
            ComPtr<IMFSample> sample;
            if (SUCCEEDED(CreateSample(token.Get(), &sample))) {
                queue->QueueEventParamUnk(MEMediaSample, GUID_NULL, S_OK, sample.Get());
            }
        }
    }

    HRESULT CreateSample(IUnknown* token, IMFSample** result) {
        ComPtr<IMFMediaBuffer> buffer;
        RETURN_IF_FAILED(MFCreateMemoryBuffer(kFrameBytes, &buffer));
        BYTE* data = nullptr;
        RETURN_IF_FAILED(buffer->Lock(&data, nullptr, nullptr));
        if (!reader_.Read(data)) {
            memset(data, 16, kWidth * kHeight);  // black in limited-range video
            memset(data + kWidth * kHeight, 128, kFrameBytes - kWidth * kHeight);
        }
        buffer->Unlock();
        RETURN_IF_FAILED(buffer->SetCurrentLength(kFrameBytes));

        ComPtr<IMFSample> sample;
        RETURN_IF_FAILED(MFCreateSample(&sample));
        RETURN_IF_FAILED(sample->AddBuffer(buffer.Get()));
        RETURN_IF_FAILED(sample->SetSampleTime(MFGetSystemTime()));
        RETURN_IF_FAILED(sample->SetSampleDuration(10000000LL / kFps));
        if (token) RETURN_IF_FAILED(sample->SetUnknown(MFSampleExtension_Token, token));
        *result = sample.Detach();
        return S_OK;
    }

    std::mutex mutex_;
    ComPtr<IMFMediaSource> source_;
    ComPtr<IMFMediaEventQueue> queue_;
    ComPtr<IMFStreamDescriptor> descriptor_;
    std::deque<ComPtr<IUnknown>> tokens_;
    MF_STREAM_STATE state_ = MF_STREAM_STATE_STOPPED;
    std::thread worker_;
    std::atomic<bool> running_{false};
    FrameReader reader_;
};

// --- source -------------------------------------------------------------------

class Source : public RuntimeClass<RuntimeClassFlags<ClassicCom>,
                                   ChainInterfaces<IMFMediaSourceEx, IMFMediaSource, IMFMediaEventGenerator>,
                                   IMFAttributes, IMFGetService, IKsControl, IMFSampleAllocatorControl> {
public:
    TRACE_QI("Source")
    Source() { ++g_objects; }
    ~Source() override { --g_objects; }

    HRESULT Initialize() {
        RETURN_IF_FAILED(MFCreateEventQueue(&queue_));
        RETURN_IF_FAILED(MFCreateAttributes(&attributes_, 1));
        stream_ = Make<Stream>();
        if (!stream_) return E_OUTOFMEMORY;
        RETURN_IF_FAILED(stream_->Initialize(this));
        IMFStreamDescriptor* descriptors[] = {stream_->Descriptor()};
        RETURN_IF_FAILED(MFCreatePresentationDescriptor(1, descriptors, &presentation_));
        return presentation_->SelectStream(0);
    }

    // IMFMediaEventGenerator
    STDMETHODIMP BeginGetEvent(IMFAsyncCallback* callback, IUnknown* state) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->BeginGetEvent(callback, state) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP EndGetEvent(IMFAsyncResult* result, IMFMediaEvent** event) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->EndGetEvent(result, event) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP GetEvent(DWORD flags, IMFMediaEvent** event) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->GetEvent(flags, event) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP QueueEvent(MediaEventType type, REFGUID extended, HRESULT status,
                            const PROPVARIANT* value) override {
        ComPtr<IMFMediaEventQueue> queue = Queue();
        return queue ? queue->QueueEventParamVar(type, extended, status, value) : MF_E_SHUTDOWN;
    }

    // IMFMediaSource
    STDMETHODIMP GetCharacteristics(DWORD* characteristics) override {
        if (!characteristics) return E_POINTER;
        *characteristics = MFMEDIASOURCE_IS_LIVE;
        return Alive() ? S_OK : MF_E_SHUTDOWN;
    }
    STDMETHODIMP CreatePresentationDescriptor(IMFPresentationDescriptor** result) override {
        if (!result) return E_POINTER;
        std::lock_guard lock(mutex_);
        return presentation_ ? presentation_->Clone(result) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP Start(IMFPresentationDescriptor* descriptor, const GUID* format,
                       const PROPVARIANT* startTime) override {
        if (!descriptor || !startTime) return E_POINTER;
        if (format && *format != GUID_NULL) return MF_E_UNSUPPORTED_TIME_FORMAT;
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;

        PROPVARIANT time;
        PropVariantInit(&time);
        time.vt = VT_I8;
        time.hVal.QuadPart = MFGetSystemTime();

        RETURN_IF_FAILED(queue_->QueueEventParamUnk(MENewStream, GUID_NULL, S_OK,
                                                    static_cast<IMFMediaStream2*>(stream_.Get())));
        RETURN_IF_FAILED(queue_->QueueEventParamVar(MESourceStarted, GUID_NULL, S_OK, &time));
        return stream_->Start(&time);
    }
    STDMETHODIMP Stop() override {
        std::lock_guard lock(mutex_);
        if (!queue_) return MF_E_SHUTDOWN;
        RETURN_IF_FAILED(queue_->QueueEventParamVar(MESourceStopped, GUID_NULL, S_OK, nullptr));
        return stream_->Stop();
    }
    STDMETHODIMP Pause() override { return MF_E_INVALID_STATE_TRANSITION; }
    STDMETHODIMP Shutdown() override {
        ComPtr<Stream> stream;
        {
            std::lock_guard lock(mutex_);
            if (!queue_) return MF_E_SHUTDOWN;
            queue_->Shutdown();
            queue_.Reset();
            presentation_.Reset();
            attributes_.Reset();
            stream = stream_;
            stream_.Reset();
        }
        stream->Shutdown();  // outside the lock: this waits for the worker thread
        return S_OK;
    }

    // IMFMediaSourceEx
    STDMETHODIMP GetSourceAttributes(IMFAttributes** attributes) override {
        if (!attributes) return E_POINTER;
        std::lock_guard lock(mutex_);
        return attributes_ ? attributes_.CopyTo(attributes) : MF_E_SHUTDOWN;
    }
    STDMETHODIMP GetStreamAttributes(DWORD id, IMFAttributes** attributes) override {
        if (!attributes) return E_POINTER;
        if (id != 0) return MF_E_NOT_FOUND;
        std::lock_guard lock(mutex_);
        if (!stream_) return MF_E_SHUTDOWN;
        return stream_->Descriptor()->QueryInterface(IID_PPV_ARGS(attributes));
    }
    STDMETHODIMP SetD3DManager(IUnknown*) override { return S_OK; }

    // IMFAttributes: Frame Server reads these from the source object directly.
    FORWARD_ATTRIBUTES(attributes_)

    // IMFGetService
    STDMETHODIMP GetService(REFGUID, REFIID, LPVOID*) override {
        return MF_E_UNSUPPORTED_SERVICE;
    }

    // IMFSampleAllocatorControl: frames are plain memory buffers made here.
    STDMETHODIMP SetDefaultAllocator(DWORD, IUnknown*) override { return S_OK; }
    STDMETHODIMP GetAllocatorUsage(DWORD id, DWORD* inputId, MFSampleAllocatorUsage* usage) override {
        if (!inputId || !usage) return E_POINTER;
        if (id != 0) return MF_E_NOT_FOUND;
        *inputId = 0;
        *usage = MFSampleAllocatorUsage_DoesNotAllocate;
        return S_OK;
    }

    // IKsControl
    STDMETHODIMP KsProperty(PKSPROPERTY, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }
    STDMETHODIMP KsMethod(PKSMETHOD, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }
    STDMETHODIMP KsEvent(PKSEVENT, ULONG, LPVOID, ULONG, ULONG*) override {
        return HRESULT_FROM_WIN32(ERROR_SET_NOT_FOUND);
    }

private:
    ComPtr<IMFMediaEventQueue> Queue() {
        std::lock_guard lock(mutex_);
        return queue_;
    }
    bool Alive() {
        std::lock_guard lock(mutex_);
        return queue_ != nullptr;
    }

    std::mutex mutex_;
    ComPtr<IMFMediaEventQueue> queue_;
    ComPtr<IMFAttributes> attributes_;
    ComPtr<IMFPresentationDescriptor> presentation_;
    ComPtr<Stream> stream_;
};

// --- activation ---------------------------------------------------------------

class Activator : public RuntimeClass<RuntimeClassFlags<ClassicCom>, ChainInterfaces<IMFActivate, IMFAttributes>> {
public:
    TRACE_QI("Activator")
    Activator() { ++g_objects; }
    ~Activator() override { --g_objects; }

    HRESULT Initialize() {
        RETURN_IF_FAILED(MFCreateAttributes(&attributes_, 2));
        RETURN_IF_FAILED(attributes_->SetUINT32(MF_VIRTUALCAMERA_PROVIDE_ASSOCIATED_CAMERA_SOURCES, 1));
        return attributes_->SetGUID(MFT_TRANSFORM_CLSID_Attribute, CLSID_NvbbCamera);
    }

    FORWARD_ATTRIBUTES(attributes_)

    STDMETHODIMP ActivateObject(REFIID riid, void** result) override {
        Trace("ActivateObject", &riid, S_OK);
        if (!result) return E_POINTER;
        std::lock_guard lock(mutex_);
        if (!source_) {
            source_ = Make<Source>();
            if (!source_) return E_OUTOFMEMORY;
            RETURN_IF_FAILED(source_->Initialize());
        }
        return source_->QueryInterface(riid, result);
    }
    STDMETHODIMP ShutdownObject() override {
        std::lock_guard lock(mutex_);
        if (source_) source_->Shutdown();
        source_.Reset();
        return S_OK;
    }
    STDMETHODIMP DetachObject() override {
        std::lock_guard lock(mutex_);
        source_.Reset();
        return S_OK;
    }

private:
    std::mutex mutex_;
    ComPtr<IMFAttributes> attributes_;
    ComPtr<Source> source_;
};

class ClassFactory : public RuntimeClass<RuntimeClassFlags<ClassicCom>, IClassFactory> {
public:
    STDMETHODIMP CreateInstance(IUnknown* outer, REFIID riid, void** result) override {
        if (outer) return CLASS_E_NOAGGREGATION;
        ComPtr<Activator> activator = Make<Activator>();
        if (!activator) return E_OUTOFMEMORY;
        RETURN_IF_FAILED(activator->Initialize());
        return activator->QueryInterface(riid, result);
    }
    STDMETHODIMP LockServer(BOOL lock) override {
        lock ? ++g_objects : --g_objects;
        return S_OK;
    }
};

// --- DLL entry points ---------------------------------------------------------

BOOL WINAPI DllMain(HINSTANCE instance, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) {
        g_module = instance;
        DisableThreadLibraryCalls(instance);
    }
    return TRUE;
}

STDAPI DllGetClassObject(REFCLSID clsid, REFIID riid, void** result) {
    Trace("DllGetClassObject", &riid, S_OK);
    if (clsid != CLSID_NvbbCamera) return CLASS_E_CLASSNOTAVAILABLE;
    ComPtr<ClassFactory> factory = Make<ClassFactory>();
    return factory ? factory->QueryInterface(riid, result) : E_OUTOFMEMORY;
}

STDAPI DllCanUnloadNow() { return g_objects.load() == 0 ? S_OK : S_FALSE; }

STDAPI DllRegisterServer() {
    wchar_t path[MAX_PATH];
    if (!GetModuleFileNameW(g_module, path, MAX_PATH)) return HRESULT_FROM_WIN32(GetLastError());
    const std::wstring key = std::wstring(L"Software\\Classes\\CLSID\\") + kClsidString + L"\\InprocServer32";
    HKEY handle;
    LSTATUS status = RegCreateKeyExW(HKEY_LOCAL_MACHINE, key.c_str(), 0, nullptr, 0, KEY_SET_VALUE,
                                     nullptr, &handle, nullptr);
    if (status != ERROR_SUCCESS) return HRESULT_FROM_WIN32(status);
    static const wchar_t threading[] = L"Both";
    status = RegSetValueExW(handle, nullptr, 0, REG_SZ, reinterpret_cast<const BYTE*>(path),
                            DWORD((wcslen(path) + 1) * sizeof(wchar_t)));
    if (status == ERROR_SUCCESS) {
        status = RegSetValueExW(handle, L"ThreadingModel", 0, REG_SZ,
                                reinterpret_cast<const BYTE*>(threading), sizeof(threading));
    }
    RegCloseKey(handle);
    return HRESULT_FROM_WIN32(status);
}

STDAPI DllUnregisterServer() {
    const std::wstring key = std::wstring(L"Software\\Classes\\CLSID\\") + kClsidString;
    const LSTATUS status = RegDeleteTreeW(HKEY_LOCAL_MACHINE, key.c_str());
    return status == ERROR_FILE_NOT_FOUND ? S_OK : HRESULT_FROM_WIN32(status);
}
