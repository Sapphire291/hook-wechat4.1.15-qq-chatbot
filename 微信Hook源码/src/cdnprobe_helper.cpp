#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsock2.h>
#pragma comment(lib,"ws2_32.lib")
#include <cstdio>
#include <cstdint>
#include <string>
static volatile LONG64 g_lastTaskObj;
static volatile LONG64 g_lastMgrObj;
#include "MinHook.h"

static void Log(const char* fmt, ...)
{
    char buf[4096] = {0};
    va_list ap; va_start(ap, fmt);
    _vsnprintf_s(buf, sizeof(buf)-2, _TRUNCATE, fmt, ap);
    va_end(ap);
    SYSTEMTIME st; GetLocalTime(&st);
    FILE* f = nullptr;
    if (_wfopen_s(&f, L"E:\\CHAT\\cdnprobe_helper.log", L"a, ccs=UTF-8") == 0 && f) {
        fwprintf(f, L"[%02d:%02d:%02d.%03d %lu] %hs\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, GetCurrentProcessId(), buf);
        fclose(f);
    }
}

static volatile LONG g_teaHit = 0;
using FnCreate = void(*)(uint64_t, uint64_t);
static FnCreate g_origCreateA = nullptr;
static FnCreate g_origCreateB = nullptr;
static volatile LONG g_hitA = 0, g_hitB = 0, g_taskSeen = 0;

static void DumpObj(const char* tag, uint64_t obj)
{
    if (obj < 0x10000) return;
    __try {
        uint64_t vt = *(uint64_t*)obj;
        uint64_t p68 = *(uint64_t*)(obj+0x68);
        uint64_t l78 = *(uint64_t*)(obj+0x78);
        uint64_t p88 = *(uint64_t*)(obj+0x88);
        uint64_t l98 = *(uint64_t*)(obj+0x98);
        uint64_t pA8 = *(uint64_t*)(obj+0xA8);
        uint64_t lB8 = *(uint64_t*)(obj+0xB8);
        Log("[%s] obj=%llX vt=%llX p68=%llX l78=%llX p88=%llX l98=%llX pA8=%llX lB8=%llX",
            tag, (unsigned long long)obj, (unsigned long long)vt,
            (unsigned long long)p68, (unsigned long long)l78,
            (unsigned long long)p88, (unsigned long long)l98,
            (unsigned long long)pA8, (unsigned long long)lB8);
        if (p88 > 0x10000 && l98 > 0 && l98 < 1024) {
            char s[1025] = {0};
            memcpy(s, (void*)p88, (size_t)l98);
            Log("[%s] fileid=%s", tag, s);
        }
        if (p68 > 0x10000 && l78 > 0 && l78 < 1024) {
            char hex[2049]={0}; unsigned char* p=(unsigned char*)p68;
            for (size_t i=0;i<(size_t)l78 && i<1024;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
            Log("[%s] f68=%s", tag, hex);
        }
        if (pA8 > 0x10000 && lB8 > 0 && lB8 < 1024) {
            char hex[2049]={0}; unsigned char* p=(unsigned char*)pA8;
            for (size_t i=0;i<(size_t)lB8 && i<1024;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
            Log("[%s] fA8=%s", tag, hex);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        Log("[%s] dump EXC", tag);
    }
}

static void DetourCreateA(uint64_t a1, uint64_t a2)
{
    LONG n = InterlockedIncrement(&g_hitA); InterlockedExchange(&g_taskSeen, 1);
    Log("[createA] hit#%ld a1=%llX a2=%llX", n, (unsigned long long)a1, (unsigned long long)a2);
    if (n <= 20) DumpObj("createA", a1);
    g_origCreateA(a1, a2);
}
static void DetourCreateB(uint64_t a1, uint64_t a2)
{
    g_lastTaskObj=(LONG64)a1;
    LONG n = InterlockedIncrement(&g_hitB); InterlockedExchange(&g_taskSeen, 1); InterlockedExchange(&g_teaHit, 0);
    Log("[createB] hit#%ld a1=%llX a2=%llX", n, (unsigned long long)a1, (unsigned long long)a2);
    if (n <= 20) DumpObj("createB", a1);
    g_origCreateB(a1, a2);
    if (n <= 20) {
        Sleep(25);
        DumpObj("createB_post", a1);
    }
}

using FnTea = uint64_t(*)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
static FnTea g_origTea = nullptr;
static uint64_t DetourTea(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_teaHit);
    if (g_taskSeen) {
        Log("[tea] hit#%ld p1=%llX p2=%llX len=%llu", n,
            (unsigned long long)a1, (unsigned long long)a2, (unsigned long long)a3);
        if (a2 > 0x10000 && a3 > 0 && a3 <= 1024) {
            __try {
                unsigned char* p = (unsigned char*)a2;
                char hex[2049] = {0};
                size_t lim = (a3 < 512) ? (size_t)a3 : 512;
                for (size_t i=0;i<lim;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
                Log("[tea] input=%s", hex);
            } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[tea] input EXC"); }
        }
    }
    uint64_t r = g_origTea(a1,a2,a3,a4,a5);
    if (g_taskSeen) {
        __try {
            uint64_t op = *(uint64_t*)a4;
            uint64_t ol = *(uint64_t*)(a4 + 0x10);
            Log("[tea] out#%ld ptr=%llX len=%llu", n, (unsigned long long)op, (unsigned long long)ol);
            if (op > 0x10000 && ol > 0 && ol <= 4096) {
                unsigned char* p = (unsigned char*)op;
                char hex[8193] = {0};
                size_t lim = (ol < 1024) ? (size_t)ol : 1024;
                for (size_t i=0;i<lim;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
                Log("[tea] output=%s", hex);
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[tea] out EXC"); }
    }
    return r;
}


using FnPacker = uint64_t(*)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
static FnPacker g_origPacker = nullptr;
static volatile LONG g_packerHit = 0;
static uint64_t DetourPacker(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_packerHit);
    if (n <= 40) {
        Log("[packer] hit#%ld a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX", n,
            (unsigned long long)a1, (unsigned long long)a2, (unsigned long long)a3,
            (unsigned long long)a4, (unsigned long long)a5);
        uint64_t ps[5] = {a1,a2,a3,a4,a5};
        for (int k=0;k<5;k++) {
            __try {
                char s[257] = {0}; memcpy(s, (void*)ps[k], 256);
                bool pr = true; for (int i=0;i<256 && s[i];i++) if ((unsigned char)s[i]<32 || (unsigned char)s[i]>126) { pr=false; break; }
                if (pr && s[0]) Log("[packer] str%d=%s", k, s);
                else {
                    char hex[513]={0}; for(int i=0;i<128;i++) sprintf_s(hex+i*2,3,"%02X",((unsigned char*)ps[k])[i]);
                    Log("[packer] hex%d=%s", k, hex);
                }
            } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[packer] dump%d EXC", k); }
        }
    }
    return g_origPacker(a1,a2,a3,a4,a5);
}

using FnBigPacker = uint64_t(*)(uint64_t, uint64_t);
static FnBigPacker g_origBigPacker = nullptr;
static volatile LONG g_bigPackerHit = 0;
static uint64_t DetourBigPacker(uint64_t a1, uint64_t a2)
{
    LONG n = InterlockedIncrement(&g_bigPackerHit);
    if (n <= 30) {
        Log("[bigpack] hit#%ld a1=%llX a2=%llX", n, (unsigned long long)a1, (unsigned long long)a2);
        uint64_t ps[2] = {a1,a2};
        for (int k=0;k<2;k++) {
            __try {
                unsigned char* p = (unsigned char*)ps[k];
                char hex[513] = {0};
                for (int i=0;i<128;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
                Log("[bigpack] hex%d=%s", k, hex);
                char str[257] = {0};
                memcpy(str, p, 256);
                bool ok = true;
                for (int i=0;i<256 && str[i];i++) if ((unsigned char)str[i] < 32 || (unsigned char)str[i] > 126) { ok=false; break; }
                if (ok && str[0]) Log("[bigpack] str%d=%s", k, str);
            } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[bigpack] dump%d EXC", k); }
        }
    }
    uint64_t r = g_origBigPacker(a1,a2);
    if (n <= 30) {
        Log("[bigpack] RETURN hit#%ld r=%llX", n, (unsigned long long)r);
        __try {
            if (r > 0x10000) {
                uint64_t rp = *(uint64_t*)r;
                uint64_t rl = *(uint64_t*)(r + 0x10);
                Log("[bigpack] retobj ptr=%llX len=%llu", (unsigned long long)rp, (unsigned long long)rl);
                if (rp > 0x10000 && rl > 0 && rl <= 4096) {
                    unsigned char* p = (unsigned char*)rp;
                    char hex[8193] = {0};
                    size_t lim = (rl < 1024) ? (size_t)rl : 1024;
                    for (size_t i=0;i<lim;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
                    Log("[bigpack] ret=%s", hex);
                }
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[bigpack] ret EXC"); }
    }
    return r;
}


static void DumpArgStrings(const char* tag, uint64_t p);
using FnTaskStart = uint64_t(*)(uint64_t,uint64_t,uint64_t,uint64_t,uint64_t,uint64_t,uint64_t);
static FnTaskStart g_origTaskStart = nullptr;
static volatile LONG g_taskStartHit = 0;
static uint64_t DetourTaskStart(uint64_t a1,uint64_t a2,uint64_t a3,uint64_t a4,uint64_t a5,uint64_t a6,uint64_t a7)
{
    LONG n = InterlockedIncrement(&g_taskStartHit);
    if (n <= 30) {
        Log("[taskstart] hit#%ld a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX a6=%llX a7=%llX", n,
            (unsigned long long)a1,(unsigned long long)a2,(unsigned long long)a3,(unsigned long long)a4,
            (unsigned long long)a5,(unsigned long long)a6,(unsigned long long)a7);
        DumpArgStrings("a2", a2);
        DumpArgStrings("a3", a3);
        DumpArgStrings("a4", a4);
        DumpArgStrings("a5", a5);
        DumpArgStrings("a6", a6);
        for (int k=0;k<4;k++) {
            uint64_t pv = (&a1)[k];
            __try {
                unsigned char* p=(unsigned char*)pv; char hex[513]={0};
                for(int i=0;i<128;i++) sprintf_s(hex+i*2,3,"%02X",p[i]);
                Log("[taskstart] hex%d=%s",k,hex);
            } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[taskstart] dump%d EXC",k); }
        }
    }
    return g_origTaskStart(a1,a2,a3,a4,a5,a6,a7);
}


static void DumpArgStrings(const char* tag, uint64_t p)
{
    if (p < 0x10000) { Log("[taskstart] %s invalid", tag); return; }
    unsigned char buf[4096] = {0};
    __try { memcpy(buf, (void*)p, 4095); }
    __except (EXCEPTION_EXECUTE_HANDLER) { Log("[taskstart] %s read EXC", tag); return; }
    int i = 0;
    while (i < 4095) {
        if (buf[i] >= 32 && buf[i] < 127) {
            int j = i;
            while (j < 4095 && buf[j] >= 32 && buf[j] < 127) j++;
            if (j - i >= 4) {
                char out[257] = {0};
                int n = j - i; if (n > 240) n = 240;
                memcpy(out, buf + i, n); out[n] = 0;
                Log("[taskstart] %s str@%03X=%s", tag, i, out);
            }
            i = j;
        } else i++;
    }
    Log("[taskstart] %s head=%02X%02X%02X%02X %02X%02X%02X%02X",
        tag, buf[0],buf[1],buf[2],buf[3],buf[4],buf[5],buf[6],buf[7]);
}


// ===== three network directions: WSASend / send / WSASendTo =====
static void LogNetBuf(const char* tag, const char* buf, int len)
{
    if (!buf || len <= 0) { Log("[net:%s] empty", tag); return; }
    int lim = (len < 256) ? len : 256;
    char hex[521] = {0};
    for (int i=0;i<lim;i++) sprintf_s(hex+i*2,3,"%02X",(unsigned char)buf[i]);
    Log("[net:%s] len=%d %s", tag, len, hex);
}

using FnWSASend = int (WSAAPI *)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE);
static FnWSASend g_origWSASend = nullptr;
static volatile LONG g_wsasendHit = 0;
static int WSAAPI DetourWSASend(SOCKET s, LPWSABUF bufs, DWORD cnt, LPDWORD sent, DWORD flags, LPWSAOVERLAPPED ov, LPWSAOVERLAPPED_COMPLETION_ROUTINE cb)
{
    LONG n = InterlockedIncrement(&g_wsasendHit);
    if (n <= 80) {
        Log("[net:wsasend] hit#%ld s=%llu cnt=%lu", n, (unsigned long long)(uintptr_t)s, (unsigned long)cnt);
        for (DWORD i=0;i<cnt && i<4;i++) LogNetBuf("wsasend", bufs[i].buf, (int)bufs[i].len);
    }
    return g_origWSASend(s, bufs, cnt, sent, flags, ov, cb);
}

using FnSend = int (WSAAPI *)(SOCKET, const char*, int, int);
static FnSend g_origSend = nullptr;
static volatile LONG g_sendHit = 0;
static int WSAAPI DetourSend(SOCKET s, const char* buf, int len, int flags)
{
    LONG n = InterlockedIncrement(&g_sendHit);
    if (n <= 80) { Log("[net:send] hit#%ld s=%llu", n, (unsigned long long)(uintptr_t)s); LogNetBuf("send", buf, len); }
    return g_origSend(s, buf, len, flags);
}

using FnWSASendTo = int (WSAAPI *)(SOCKET, LPWSABUF, DWORD, LPDWORD, DWORD, const struct sockaddr*, int, LPWSAOVERLAPPED, LPWSAOVERLAPPED_COMPLETION_ROUTINE);
static FnWSASendTo g_origWSASendTo = nullptr;
static volatile LONG g_wsasendtoHit = 0;
static int WSAAPI DetourWSASendTo(SOCKET s, LPWSABUF bufs, DWORD cnt, LPDWORD sent, DWORD flags, const struct sockaddr* to, int tolen, LPWSAOVERLAPPED ov, LPWSAOVERLAPPED_COMPLETION_ROUTINE cb)
{
    LONG n = InterlockedIncrement(&g_wsasendtoHit);
    if (n <= 80) {
        Log("[net:wsasendto] hit#%ld s=%llu cnt=%lu tolen=%d", n, (unsigned long long)(uintptr_t)s, (unsigned long)cnt, tolen);
        for (DWORD i=0;i<cnt && i<4;i++) LogNetBuf("wsasendto", bufs[i].buf, (int)bufs[i].len);
    }
    return g_origWSASendTo(s, bufs, cnt, sent, flags, to, tolen, ov, cb);
}

static void RegisterNetProbes()
{
    HMODULE ws = GetModuleHandleW(L"ws2_32.dll");
    if (!ws) ws = LoadLibraryW(L"ws2_32.dll");
    if (!ws) { Log("[net] ws2_32 not loaded"); return; }
    auto reg = [&](const char* name, LPVOID det, LPVOID* orig) {
        FARPROC p = GetProcAddress(ws, name);
        if (!p) { Log("[net] %s missing", name); return; }
        MH_STATUS st = MH_CreateHook((LPVOID)p, det, orig);
        Log("[net] hook %s st=%d target=%p", name, (int)st, (void*)p);
        if (st == MH_OK) Log("[net] enable %s st=%d", name, (int)MH_EnableHook((LPVOID)p));
    };
    reg("WSASend", (LPVOID)&DetourWSASend, (LPVOID*)&g_origWSASend);
    reg("send", (LPVOID)&DetourSend, (LPVOID*)&g_origSend);
    reg("WSASendTo", (LPVOID)&DetourWSASendTo, (LPVOID*)&g_origWSASendTo);
}


// ===== Cronet URL request pre-encryption probes =====
static void DumpArgStrings(const char* tag, uint64_t p);
using FnUrlInit = uint64_t(*)(uint64_t,uint64_t,const char*,uint64_t,uint64_t);
static FnUrlInit g_origUrlInit = nullptr;
static volatile LONG g_urlInitHit = 0;
static uint64_t DetourUrlInit(uint64_t a1,uint64_t a2,const char* url,uint64_t a4,uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_urlInitHit);
    if (n <= 100) {
        Log("[cronet] init#%ld req=%llX engine=%llX url=%s", n,
            (unsigned long long)a1,(unsigned long long)a2, url ? url : "(null)");
    }
    return g_origUrlInit(a1,a2,url,a4,a5);
}
using FnUrlStart = uint64_t(*)(uint64_t);
static FnUrlStart g_origUrlStart = nullptr;
static volatile LONG g_urlStartHit = 0;
static uint64_t DetourUrlStart(uint64_t a1)
{
    LONG n = InterlockedIncrement(&g_urlStartHit);
    if (n <= 100) { DumpArgStrings("cronetreq", a1); Log("[cronet] start#%ld req=%llX", n, (unsigned long long)a1); }
    return g_origUrlStart(a1);
}
static void RegisterCronetProbes()
{
    HMODULE m = GetModuleHandleW(L"mmcronet.dll");
    if (!m) { Log("[cronet] mmcronet not loaded"); return; }
    auto reg = [&](const char* name, LPVOID det, LPVOID* orig) {
        FARPROC p = GetProcAddress(m, name);
        if (!p) { Log("[cronet] %s missing", name); return; }
        MH_STATUS st = MH_CreateHook((LPVOID)p, det, orig);
        Log("[cronet] hook %s st=%d target=%p", name, (int)st, (void*)p);
        if (st == MH_OK) Log("[cronet] enable %s st=%d", name, (int)MH_EnableHook((LPVOID)p));
    };
    reg("Cronet_UrlRequest_InitWithParams", (LPVOID)&DetourUrlInit, (LPVOID*)&g_origUrlInit);
    reg("Cronet_UrlRequest_Start", (LPVOID)&DetourUrlStart, (LPVOID*)&g_origUrlStart);
}


// ===== legacy CdnManager probes (safe, no candidates) =====
using FnCdn2 = uint64_t(*)(uint64_t, uint32_t);
static FnCdn2 g_origCdn = nullptr;
static FnCdn2 g_origCdnW = nullptr;
static volatile LONG g_cdnHit = 0, g_cdnWHit = 0;
static uint64_t DetourCdn(uint64_t a1, uint32_t a2)
{
    LONG n = InterlockedIncrement(&g_cdnHit);
    if (n <= 20) Log("[cdn] hit#%ld a1=%llX a2=%u", n, (unsigned long long)a1, a2);
    return g_origCdn(a1, a2);
}
static uint64_t DetourCdnW(uint64_t a1, uint32_t a2)
{
    LONG n = InterlockedIncrement(&g_cdnWHit);
    if (n <= 20) Log("[cdnw] hit#%ld a1=%llX a2=%u", n, (unsigned long long)a1, a2);
    return g_origCdnW(a1, a2);
}
static void RegisterCdnManagerProbes()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) return;
    auto reg = [&](const char* name, uintptr_t rva, LPVOID det, LPVOID* orig) {
        void* target = (void*)((uintptr_t)wx + rva);
        MH_STATUS st = MH_CreateHook(target, det, orig);
        Log("[cdn] hook %s rva=%llX st=%d", name, (unsigned long long)rva, (int)st);
        if (st == MH_OK) Log("[cdn] enable %s st=%d", name, (int)MH_EnableHook(target));
    };
    reg("cdnstart", 0x23d78f0, (LPVOID)&DetourCdn, (LPVOID*)&g_origCdn);
    reg("cdnwrap", 0x22fb320, (LPVOID)&DetourCdnW, (LPVOID*)&g_origCdnW);
}


// ===== extra create-caller / Cronet create probes =====
using FnCreateCaller = uint64_t(*)(uint64_t, uint64_t);
static FnCreateCaller g_origCreateCaller = nullptr;
static volatile LONG g_createCallerHit = 0;
static uint64_t DetourCreateCaller(uint64_t a1, uint64_t a2)
{
    { __try { uint64_t _m=*(uint64_t*)(a2+0x38); if(_m>0x10000) g_lastMgrObj=(LONG64)_m; } __except(EXCEPTION_EXECUTE_HANDLER) {} }
    LONG n = InterlockedIncrement(&g_createCallerHit);
    if (n <= 30) {
        Log("[createcaller] hit#%ld a1=%llX a2=%llX", n,
            (unsigned long long)a1, (unsigned long long)a2);
        __try {
            uint64_t mgr = *(uint64_t*)(a2 + 0x38);
            Log("[createcaller] mgr=%llX", (unsigned long long)mgr);
            if (mgr > 0x10000) {
                uint64_t vt = *(uint64_t*)mgr;
                Log("[createcaller] mgr_vt=%llX", (unsigned long long)vt);
                for (int i = 0; i < 16; ++i) {
                    uint64_t fn = 0;
                    __try { fn = *(uint64_t*)(vt + i*8); } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
                    Log("[createcaller] vt[%02X]=%llX", i*8, (unsigned long long)fn);
                }
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) { Log("[createcaller] dump EXC"); }
    }
    return g_origCreateCaller(a1, a2);
}

using FnCronetCreate = uint64_t(*)(uint64_t,uint64_t,uint64_t,uint64_t,uint64_t);
static FnCronetCreate g_origCronetCreate = nullptr;
static FnCronetCreate g_origCronetCreateWith = nullptr;
static volatile LONG g_cronetCreateHit = 0;
static uint64_t DetourCronetCreate(uint64_t a1,uint64_t a2,uint64_t a3,uint64_t a4,uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_cronetCreateHit);
    if (n <= 60) Log("[cronet-create] hit#%ld a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX",
                     n,(unsigned long long)a1,(unsigned long long)a2,(unsigned long long)a3,
                     (unsigned long long)a4,(unsigned long long)a5);
    return g_origCronetCreate(a1,a2,a3,a4,a5);
}
static uint64_t DetourCronetCreateWith(uint64_t a1,uint64_t a2,uint64_t a3,uint64_t a4,uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_cronetCreateHit);
    if (n <= 60) Log("[cronet-createwith] hit#%ld a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX",
                     n,(unsigned long long)a1,(unsigned long long)a2,(unsigned long long)a3,
                     (unsigned long long)a4,(unsigned long long)a5);
    return g_origCronetCreateWith(a1,a2,a3,a4,a5);
}
static void RegisterExtraProbes()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (wx) {
        void* t = (void*)((uintptr_t)wx + 0x3b56a0);
        MH_STATUS st = MH_CreateHook(t, (LPVOID)&DetourCreateCaller, (LPVOID*)&g_origCreateCaller);
        Log("[createcaller] hook st=%d", (int)st);
        if (st == MH_OK) Log("[createcaller] enable st=%d", (int)MH_EnableHook(t));
    }
    HMODULE m = GetModuleHandleW(L"mmcronet.dll");
    if (!m) return;
    auto reg = [&](const char* name, LPVOID det, LPVOID* orig) {
        FARPROC p = GetProcAddress(m, name);
        if (!p) return;
        MH_STATUS st = MH_CreateHook((LPVOID)p, det, orig);
        Log("[cronet-create] hook %s st=%d", name, (int)st);
        if (st == MH_OK) Log("[cronet-create] enable %s st=%d", name, (int)MH_EnableHook((LPVOID)p));
    };
    reg("Cronet_UrlRequest_Create", (LPVOID)&DetourCronetCreate, (LPVOID*)&g_origCronetCreate);
    reg("Cronet_UrlRequest_CreateWith", (LPVOID)&DetourCronetCreateWith, (LPVOID*)&g_origCronetCreateWith);
}


// ===== 100Hz task/manager monitor =====
static volatile LONG g_mgr300Hit = 0;
using FnMgr300 = void(*)(uint64_t, uint64_t);
static FnMgr300 g_origMgr300 = nullptr;
static void DetourMgr300(uint64_t a1, uint64_t a2)
{
    LONG n = InterlockedIncrement(&g_mgr300Hit);
    if (n <= 40) Log("[mgr300] hit#%ld a1=%llX a2=%llX", n,
                     (unsigned long long)a1, (unsigned long long)a2);
    g_origMgr300(a1, a2);
}
static void RegMgr300()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) return;
    void* t = (void*)((uintptr_t)wx + 0x3303a0);
    MH_STATUS st = MH_CreateHook(t, (LPVOID)&DetourMgr300, (LPVOID*)&g_origMgr300);
    Log("[mgr300] hook st=%d", (int)st);
    if (st == MH_OK) Log("[mgr300] enable st=%d", (int)MH_EnableHook(t));
}



static bool LooksLikeFileTask(uint64_t t)
{
    if (t < 0x10000) return false;
    uint64_t p=0,l=0;
    __try { p=*(uint64_t*)(t+0x88); l=*(uint64_t*)(t+0x98); } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
    if (p < 0x10000 || l < 100 || l > 400) return false;
    char s[8]={0};
    __try { memcpy(s,(void*)p,4); } __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
    return memcmp(s,"305f",4)==0;
}
// ===== manager start probe 0x4A6390 =====
using FnMgrStart = void(*)(uint64_t, uint64_t);
static FnMgrStart g_origMgrStart = nullptr;
static volatile LONG g_mgrStartHit = 0;
static void DetourMgrStart(uint64_t a1, uint64_t a2)
{
    LONG n = InterlockedIncrement(&g_mgrStartHit);
    bool fileTask = LooksLikeFileTask(a2);
    if (fileTask || n <= 10) {
        Log("[mgrstart] hit#%ld file=%d mgr=%llX task=%llX", n, (int)fileTask,
            (unsigned long long)a1, (unsigned long long)a2);
        __try { if (a2 > 0x10000) DumpObj(fileTask ? "mgrstart_file" : "mgrstart", a2); }
        __except (EXCEPTION_EXECUTE_HANDLER) { Log("[mgrstart] dump EXC"); }
    }
    g_origMgrStart(a1, a2);
}
static void RegMgrStart()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) return;
    void* t = (void*)((uintptr_t)wx + 0x4a6390);
    MH_STATUS st = MH_CreateHook(t, (LPVOID)&DetourMgrStart, (LPVOID*)&g_origMgrStart);
    Log("[mgrstart] hook st=%d target=%p", (int)st, t);
    if (st == MH_OK) Log("[mgrstart] enable st=%d", (int)MH_EnableHook(t));
}


// ===== task object vtable probes =====
using FnTaskVt = void(*)(uint64_t, uint64_t);
static FnTaskVt g_origTask0 = nullptr;
static FnTaskVt g_origTask18 = nullptr;
static FnTaskVt g_origTask20 = nullptr;
static volatile LONG g_taskVtHits = 0;
static void DetourTask0(uint64_t a1,uint64_t a2){ LONG n=InterlockedIncrement(&g_taskVtHits); if(n<=30) Log("[taskvt0] hit#%ld a1=%llX a2=%llX",n,(unsigned long long)a1,(unsigned long long)a2); g_origTask0(a1,a2); }
static void DetourTask18(uint64_t a1,uint64_t a2){ LONG n=InterlockedIncrement(&g_taskVtHits); if(n<=30) Log("[taskvt18] hit#%ld a1=%llX a2=%llX",n,(unsigned long long)a1,(unsigned long long)a2); g_origTask18(a1,a2); }

static void DumpRetStack(const char* tag)
{
    uintptr_t wb=(uintptr_t)GetModuleHandleW(L"Weixin.dll");
    if(!wb) return;
    void** ra=(void**)_AddressOfReturnAddress();
    for(int i=0;i<40;i++){
        void* v=nullptr;
        __try { v=ra[i]; } __except(EXCEPTION_EXECUTE_HANDLER) { break; }
        if((uintptr_t)v>=wb && (uintptr_t)v<wb+0xC000000ULL)
            Log("[%s] ret[%d] rva=%llX",tag,i,(unsigned long long)((uintptr_t)v-wb));
    }
}
static void DetourTask20(uint64_t a1,uint64_t a2){ LONG n=InterlockedIncrement(&g_taskVtHits); if(n<=30) { Log("[taskvt20] hit#%ld a1=%llX a2=%llX",n,(unsigned long long)a1,(unsigned long long)a2); DumpRetStack("dtor"); } g_origTask20(a1,a2); }
static void RegTaskVt()
{
    HMODULE wx=GetModuleHandleW(L"Weixin.dll"); if(!wx) return;
    auto reg=[&](const char* name,uintptr_t rva,LPVOID det,LPVOID* orig){
        void* t=(void*)((uintptr_t)wx+rva); MH_STATUS st=MH_CreateHook(t,det,orig);
        Log("[taskvt] hook %s rva=%llX st=%d",name,(unsigned long long)rva,(int)st);
        if(st==MH_OK) Log("[taskvt] enable %s st=%d",name,(int)MH_EnableHook(t));
    };
    reg("clone",0x1685aa0,(LPVOID)&DetourTask0,(LPVOID*)&g_origTask0);
    reg("get18",0x1688010,(LPVOID)&DetourTask18,(LPVOID*)&g_origTask18);
    reg("dtor",0x1688020,(LPVOID)&DetourTask20,(LPVOID*)&g_origTask20);
}

static DWORD WINAPI Monitor100(LPVOID)
{
    char lastTask[512] = {0};
    char lastMgr[256] = {0};
    Log("[mon100] start");
    for (;;) {
        Sleep(10);
        uint64_t t = (uint64_t)g_lastTaskObj;
        if (t > 0x10000) {
            char cur[512] = {0};
            __try {
                uint64_t p68 = *(uint64_t*)(t+0x68);
                uint64_t l78 = *(uint64_t*)(t+0x78);
                uint64_t p88 = *(uint64_t*)(t+0x88);
                uint64_t l98 = *(uint64_t*)(t+0x98);
                uint64_t pA8 = *(uint64_t*)(t+0xA8);
                uint64_t lB8 = *(uint64_t*)(t+0xB8);
                char fk[128]={0}, id[320]={0}, ak[128]={0};
                if (p68>0x10000 && l78>0 && l78<120) memcpy(fk,(void*)p68,(size_t)l78);
                if (p88>0x10000 && l98>0 && l98<300) memcpy(id,(void*)p88,(size_t)l98);
                if (pA8>0x10000 && lB8>0 && lB8<120) memcpy(ak,(void*)pA8,(size_t)lB8);
                sprintf_s(cur,sizeof(cur),"task=%llX fileid=%s fk=%s aes=%s",(unsigned long long)t,id,fk,ak);
            } __except (EXCEPTION_EXECUTE_HANDLER) { sprintf_s(cur,sizeof(cur),"task=%llX EXC",(unsigned long long)t); }
            if (strcmp(cur,lastTask)!=0) { Log("[mon100] %s", cur); strcpy_s(lastTask,sizeof(lastTask),cur); }
        }
        uint64_t m = (uint64_t)g_lastMgrObj;
        if (m > 0x10000) {
            char cur[256]={0};
            __try {
                uint64_t vt=*(uint64_t*)m;
                sprintf_s(cur,sizeof(cur),"mgr=%llX vt=%llX",(unsigned long long)m,(unsigned long long)vt);
            } __except (EXCEPTION_EXECUTE_HANDLER) { sprintf_s(cur,sizeof(cur),"mgr=%llX EXC",(unsigned long long)m); }
            if (strcmp(cur,lastMgr)!=0) { Log("[mon100] %s", cur); strcpy_s(lastMgr,sizeof(lastMgr),cur); }
        }
    }
    return 0;
}

static DWORD WINAPI InitThread(LPVOID)
{
    Log("=== probe init start");
    HMODULE wx = nullptr;
    for (int i=0;i<600 && !wx;i++) { wx = GetModuleHandleW(L"Weixin.dll"); if (!wx) Sleep(200); }
    if (!wx) { Log("Weixin.dll not found"); return 0; }
    uintptr_t base = (uintptr_t)wx;
    Log("Weixin.dll=%p", wx);
    if (MH_Initialize() != MH_OK) { Log("MH_Initialize failed"); return 0; }
    auto reg = [&](const char* name, uintptr_t rva, LPVOID det, LPVOID* orig) {
        void* target = (void*)(base + rva);
        MH_STATUS st = MH_CreateHook(target, det, orig);
        Log("hook %s rva=%llX st=%d", name, (unsigned long long)rva, (int)st);
        if (st == MH_OK) Log("enable %s st=%d", name, (int)MH_EnableHook(target));
    };
    reg("createA", 0x1682f80, (LPVOID)&DetourCreateA, (LPVOID*)&g_origCreateA);
    reg("createB", 0x1685c60, (LPVOID)&DetourCreateB, (LPVOID*)&g_origCreateB);
    reg("tea", 0x1618470, (LPVOID)&DetourTea, (LPVOID*)&g_origTea);
    reg("packer", 0x67f9560, (LPVOID)&DetourPacker, (LPVOID*)&g_origPacker);
    reg("bigpack", 0x67e8a00, (LPVOID)&DetourBigPacker, (LPVOID*)&g_origBigPacker);
    reg("taskstart", 0x166d420, (LPVOID)&DetourTaskStart, (LPVOID*)&g_origTaskStart);
    RegisterNetProbes();
    RegisterCronetProbes();
    RegisterCdnManagerProbes();
    RegisterExtraProbes();
    RegMgr300();
    RegMgrStart();
    RegTaskVt();
    CreateThread(nullptr, 0, Monitor100, nullptr, 0, nullptr);
    Log("=== probe ready");
    return 0;
}

BOOL APIENTRY DllMain(HMODULE hModule, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(hModule);
        CreateThread(nullptr, 0, InitThread, nullptr, 0, nullptr);
    }
    return TRUE;
}








