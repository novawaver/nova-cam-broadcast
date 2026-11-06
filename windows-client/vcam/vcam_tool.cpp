// Installs, removes and tests the virtual camera.
//
//   nvbb-vcam install     register nvbb_vcam.dll beside this exe and add the camera (admin)
//   nvbb-vcam uninstall   remove both (admin)
//   nvbb-vcam probe [n]   read n frames the way an app would, and report them
#include <windows.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mferror.h>
#include <mfvirtualcamera.h>
#include <wrl/client.h>

#include <cstdio>
#include <cwchar>
#include <string>

#include "frame_file.h"

using Microsoft::WRL::ComPtr;

static const wchar_t kCameraName[] = L"NVIDIA Broadcast";
static const wchar_t kClsid[] = L"{7B3E2C1A-5D4F-4E8B-9A61-2F6C8D1E4A90}";

static int Fail(const char* what, HRESULT hr) {
    char* text = nullptr;
    FormatMessageA(FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM |
                       FORMAT_MESSAGE_IGNORE_INSERTS,
                   nullptr, hr, 0, reinterpret_cast<char*>(&text), 0, nullptr);
    fprintf(stderr, "%s failed: 0x%08lX %s\n", what, static_cast<unsigned long>(hr), text ? text : "");
    if (text) LocalFree(text);
    return 1;
}

static std::wstring DllPath() {
    wchar_t path[MAX_PATH];
    GetModuleFileNameW(nullptr, path, MAX_PATH);
    std::wstring result(path);
    return result.substr(0, result.find_last_of(L'\\') + 1) + L"nvbb_vcam.dll";
}

static HRESULT CallDllExport(const char* name) {
    HMODULE module = LoadLibraryW(DllPath().c_str());
    if (!module) return HRESULT_FROM_WIN32(GetLastError());
    auto entry = reinterpret_cast<HRESULT(STDAPICALLTYPE*)()>(GetProcAddress(module, name));
    const HRESULT hr = entry ? entry() : HRESULT_FROM_WIN32(GetLastError());
    FreeLibrary(module);
    return hr;
}

static HRESULT OpenCamera(ComPtr<IMFVirtualCamera>& camera) {
    return MFCreateVirtualCamera(MFVirtualCameraType_SoftwareCameraSource,
                                 MFVirtualCameraLifetime_System, MFVirtualCameraAccess_AllUsers,
                                 kCameraName, kClsid, nullptr, 0, &camera);
}

static int Install() {
    HRESULT hr = CallDllExport("DllRegisterServer");
    if (FAILED(hr)) return Fail("Registering the camera component", hr);
    ComPtr<IMFVirtualCamera> camera;
    hr = OpenCamera(camera);
    if (FAILED(hr)) return Fail("MFCreateVirtualCamera", hr);
    hr = camera->Start(nullptr);
    if (FAILED(hr)) return Fail("Starting the camera", hr);
    camera->Shutdown();
    puts("Virtual camera installed.");
    return 0;
}

static int Uninstall() {
    ComPtr<IMFVirtualCamera> camera;
    HRESULT hr = OpenCamera(camera);
    if (SUCCEEDED(hr)) {
        hr = camera->Remove();
        if (FAILED(hr)) Fail("Removing the camera", hr);
        camera->Shutdown();
    }
    hr = CallDllExport("DllUnregisterServer");
    if (FAILED(hr)) return Fail("Unregistering the camera component", hr);
    puts("Virtual camera removed.");
    return 0;
}

static int Probe(int frames) {
    ComPtr<IMFAttributes> query;
    HRESULT hr = MFCreateAttributes(&query, 1);
    if (FAILED(hr)) return Fail("MFCreateAttributes", hr);
    query->SetGUID(MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE, MF_DEVSOURCE_ATTRIBUTE_SOURCE_TYPE_VIDCAP_GUID);

    IMFActivate** devices = nullptr;
    UINT32 count = 0;
    hr = MFEnumDeviceSources(query.Get(), &devices, &count);
    if (FAILED(hr)) return Fail("MFEnumDeviceSources", hr);

    ComPtr<IMFActivate> device;
    wprintf(L"Cameras found: %u\n", count);
    for (UINT32 i = 0; i < count; ++i) {
        wchar_t* name = nullptr;
        UINT32 length = 0;
        if (SUCCEEDED(devices[i]->GetAllocatedString(MF_DEVSOURCE_ATTRIBUTE_FRIENDLY_NAME, &name, &length))) {
            wprintf(L"  %ls\n", name);
            // Windows appends " (Windows Virtual Camera)" to the name.
            if (wcsstr(name, kCameraName)) device = devices[i];
            CoTaskMemFree(name);
        }
        devices[i]->Release();
    }
    CoTaskMemFree(devices);
    if (!device) {
        fprintf(stderr, "The NVIDIA Broadcast camera is not listed.\n");
        return 2;
    }

    ComPtr<IMFMediaSource> source;
    hr = device->ActivateObject(IID_PPV_ARGS(&source));
    if (FAILED(hr)) return Fail("Opening the camera", hr);
    ComPtr<IMFSourceReader> reader;
    hr = MFCreateSourceReaderFromMediaSource(source.Get(), nullptr, &reader);
    if (FAILED(hr)) return Fail("MFCreateSourceReaderFromMediaSource", hr);

    ComPtr<IMFMediaType> type;
    hr = reader->GetCurrentMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, &type);
    if (FAILED(hr)) return Fail("Reading the format", hr);
    UINT32 width = 0, height = 0;
    MFGetAttributeSize(type.Get(), MF_MT_FRAME_SIZE, &width, &height);
    GUID subtype{};
    type->GetGUID(MF_MT_SUBTYPE, &subtype);
    printf("Format: %ux%u %s\n", width, height, subtype == MFVideoFormat_NV12 ? "NV12" : "other");

    LARGE_INTEGER frequency, start, end;
    QueryPerformanceFrequency(&frequency);
    QueryPerformanceCounter(&start);
    int received = 0;
    unsigned long long firstMean = 0, lastMean = 0;
    bool changed = false;
    while (received < frames) {
        DWORD flags = 0;
        ComPtr<IMFSample> sample;
        hr = reader->ReadSample(MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, nullptr, &flags, nullptr, &sample);
        if (FAILED(hr)) return Fail("Reading a frame", hr);
        if (!sample) continue;
        ComPtr<IMFMediaBuffer> buffer;
        hr = sample->ConvertToContiguousBuffer(&buffer);
        if (FAILED(hr)) return Fail("Reading a frame buffer", hr);
        BYTE* data = nullptr;
        DWORD length = 0;
        hr = buffer->Lock(&data, nullptr, &length);
        if (FAILED(hr)) return Fail("Locking a frame", hr);
        unsigned long long sum = 0;
        const DWORD luma = kWidth * kHeight;
        for (DWORD i = 0; i < luma && i < length; i += 97) sum += data[i];
        buffer->Unlock();
        const unsigned long long mean = sum / (luma / 97);
        if (received == 0) firstMean = mean;
        else if (mean != lastMean) changed = true;
        lastMean = mean;
        ++received;
    }
    QueryPerformanceCounter(&end);
    const double seconds = double(end.QuadPart - start.QuadPart) / double(frequency.QuadPart);
    printf("Frames: %d in %.2f s (%.1f FPS)\n", received, seconds, received / seconds);
    printf("Mean brightness: first %llu, last %llu, changing: %s\n", firstMean, lastMean, changed ? "yes" : "no");
    source->Shutdown();
    return 0;
}

int wmain(int argc, wchar_t** argv) {
    if (argc < 2) {
        fputs("usage: nvbb-vcam install | uninstall | probe [frames]\n", stderr);
        return 64;
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    if (FAILED(hr)) return Fail("CoInitializeEx", hr);
    hr = MFStartup(MF_VERSION);
    if (FAILED(hr)) return Fail("MFStartup", hr);

    int code = 64;
    if (wcscmp(argv[1], L"install") == 0) code = Install();
    else if (wcscmp(argv[1], L"uninstall") == 0) code = Uninstall();
    else if (wcscmp(argv[1], L"probe") == 0) code = Probe(argc > 2 ? _wtoi(argv[2]) : 60);

    MFShutdown();
    CoUninitialize();
    return code;
}
