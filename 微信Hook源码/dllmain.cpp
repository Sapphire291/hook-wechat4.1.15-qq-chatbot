// dllmain.cpp
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
#include "tools.h"
#include "version_proxy.h"
#include "hideself.h"
#include "global.h"
#include "hook_log.h"
#include <MinHook.h>

int g_receive_type = 1;
int g_StartPort = 30001;
int g_MsgSendPort = 80;

static void ParseCmdLine(const std::string& cmd)
{
    const std::string key = "StartPort=";

    size_t pos = cmd.find(key);
    if (pos != std::string::npos)
    {
        pos += key.length();

        int port = 0;
        while (pos < cmd.length())
        {
            char ch = cmd[pos];
            if (ch < '0' || ch > '9')
                break;

            port = port * 10 + (ch - '0');
            pos++;
        }

        if (port > 0 && port <= 65535)
        {
            g_StartPort = port;
        }
    }


    const std::string key_1 = "RecvType=";

    size_t pos_1 = cmd.find(key_1);
    if (pos_1 != std::string::npos)
    {
        pos_1 += key_1.length();

        int receive_type = 0;
        while (pos_1 < cmd.length())
        {
            char ch = cmd[pos_1];
            if (ch < '0' || ch > '9')
                break;

            receive_type = receive_type * 10 + (ch - '0');
            pos_1++;
        }

        if (receive_type > 0 && receive_type <= 2)
        {
            g_receive_type = receive_type;
        }
    }


    // ----------------------------
    // 解析 CallBackURL
    // ----------------------------

    const std::string recv_url = "CallBackURL=";
    size_t recv_pos = cmd.find(recv_url);

    if (recv_pos != std::string::npos)
    {
        recv_pos += recv_url.length();

        std::string url = ExtractQuotedString(cmd, recv_pos);

        if (!url.empty())
        {
            g_CallBack_Url = url;

            // 默认端口
            g_MsgSendPort = 80;

            // 查找协议分隔
            size_t scheme_end = url.find("://");
            size_t host_start = (scheme_end == std::string::npos) ? 0 : scheme_end + 3;

            // 找 host:port 部分结束位置
            size_t path_pos = url.find('/', host_start);
            std::string host_port = (path_pos == std::string::npos)
                ? url.substr(host_start)
                : url.substr(host_start, path_pos - host_start);

            // 查找端口
            size_t colon_pos = host_port.find(':');
            if (colon_pos != std::string::npos)
            {
                std::string port_str = host_port.substr(colon_pos + 1);

                int port = atoi(port_str.c_str());
                if (port > 0 && port <= 65535)
                {
                    g_MsgSendPort = port;
                }
            }
        }
    }

}

static std::string GetCmdLine()
{
    std::wstring wcmd = GetCommandLineW();
    return WStringToString(wcmd); 
}


static bool IsMainWeixinProcess()
{
    std::string cmd = GetCmdLine();
	//OutputDebugStringA(("[CmdLine] " + cmd + "\n").c_str());

    // 子进程特征（只要命中一个，就不是主进程）
    if (cmd.find("--type=") != std::string::npos)
        return false;

    if (cmd.find("--crashpad-handler") != std::string::npos)
        return false;

    ParseCmdLine(cmd);

    return true; // 剩下的就是主进程
}

DWORD WINAPI SelfUnloadThread(LPVOID param)
{
    HMODULE hMod = (HMODULE)param;
    Sleep(100); // 给系统一点时间
    FreeLibraryAndExitThread(hMod, 0);
    return 0;
}



// 崩溃/异常自动记录（每次失败都写入 hook_log.txt，便于事后排查）
static LONG WINAPI CrashLogger(EXCEPTION_POINTERS* ep)
{
    if (!ep || !ep->ExceptionRecord) return EXCEPTION_CONTINUE_SEARCH;
    EXCEPTION_RECORD* r = ep->ExceptionRecord;
    // 跳过：C++ EH(0xE06D7363)、调试打印(0x40010006/0x4001000A)、微软安全 cookie(0x40080201 等)
    if (r->ExceptionCode == 0xE06D7363) return EXCEPTION_CONTINUE_SEARCH;
    if (r->ExceptionCode == 0x40010006 || r->ExceptionCode == 0x4001000A) return EXCEPTION_CONTINUE_SEARCH;
    if ((r->ExceptionCode & 0xF0000000) == 0x40000000 && r->ExceptionCode != 0x40000015) return EXCEPTION_CONTINUE_SEARCH;
    // 0x6BA：微信内部高频非致命异常（UIA 跨进程调用期间大量出现）。
    // 每条 5 行日志会占用热路径 IO，拖慢 provider 导致 UIA 端 E_FAIL；只留前 3 次样本。
    if (r->ExceptionCode == 0x6BA) {
        static volatile LONG s_n6ba = 0;
        if (InterlockedIncrement(&s_n6ba) > 3) return EXCEPTION_CONTINUE_SEARCH;
    }
    // C0000005 第一轮（可继续）异常：__try 已捕获的探针投机读，只留前 5 个样本，避免热路径 IO
    if (r->ExceptionCode == 0xC0000005) {
        static volatile LONG s_nav = 0;
        if ((r->ExceptionFlags & EXCEPTION_NONCONTINUABLE) == 0) {
            if (InterlockedIncrement(&s_nav) > 5) return EXCEPTION_CONTINUE_SEARCH;
        }
    }
    // 关键：本函数内不能调用 OutputDebugString（会再次触发 0x40010006 → 递归栈溢出）
    HookLog("[crash] code=%08X addr=%p flags=%08X params=%llu",
            r->ExceptionCode, r->ExceptionAddress, r->ExceptionFlags,
            (unsigned long long)r->NumberParameters);
    for (DWORD i = 0; i < r->NumberParameters && i < 4; ++i)
        HookLog("[crash]   param[%lu]=%p", i, (void*)r->ExceptionInformation[i]);
    CONTEXT* c = ep->ContextRecord;
    if (c) {
        HookLog("[crash]   rip=%p rsp=%p rbp=%p", (void*)c->Rip, (void*)c->Rsp, (void*)c->Rbp);
        HookLog("[crash]   rcx=%p rdx=%p r8=%p r9=%p", (void*)c->Rcx, (void*)c->Rdx, (void*)c->R8, (void*)c->R9);
        HookLog("[crash]   rax=%p rbx=%p rsi=%p rdi=%p", (void*)c->Rax, (void*)c->Rbx, (void*)c->Rsi, (void*)c->Rdi);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}
static HMODULE g_hSelfModule = nullptr;
// ===== CDN task probe (read-only, no debugger) =====
using FnCdnStart = uint64_t(*)(uint64_t, uint32_t);
static FnCdnStart g_origCdnStart = nullptr;
static volatile LONG g_cdnHits = 0;

static void DumpCdnObject(uint64_t obj)
{
    HookLog("[cdn] object=%llX", (unsigned long long)obj);
    __try {
        for (int i = 0; i < 24; ++i) {
            uint64_t v = *(uint64_t*)(obj + i * 8);
            HookLog("[cdn] +%02X=%llX", i * 8, (unsigned long long)v);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        HookLog("[cdn] object read failed");
    }
    __try {
        unsigned char buf[512] = {0};
        memcpy(buf, (void*)obj, 511);
        char* s = (char*)buf;
        HookLog("[cdn] objstr=%s", s);
    } __except (EXCEPTION_EXECUTE_HANDLER) {}
}

static uint64_t DetourCdnStart(uint64_t obj, uint32_t arg)
{
    LONG n = InterlockedIncrement(&g_cdnHits);
    HookLog("[cdn] ENTER hit#%ld rcx=%llX rdx=%u", n,
            (unsigned long long)obj, arg);
    if (n <= 20 && obj > 0x10000) {
        __try { DumpCdnObject(obj); }
        __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[cdn] dump EXC"); }
    }
    __try {
        uint64_t r = g_origCdnStart(obj, arg);
        HookLog("[cdn] RETURN hit#%ld r=%llX", n, (unsigned long long)r);
        return r;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        HookLog("[cdn] CALL EXC hit#%ld code=%08X", n, GetExceptionCode());
        return 0;
    }
}

static void RegisterCdnProbe()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) { HookLog("[cdn] Weixin.dll null"); return; }
    void* target = (void*)((uintptr_t)wx + 0x23d78f0);
    MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourCdnStart, (LPVOID*)&g_origCdnStart);
    HookLog("[cdn] MH_CreateHook(%p) st=%d orig=%p", target, (int)st, (void*)g_origCdnStart);
    if (st == MH_OK) {
        MH_STATUS st2 = MH_EnableHook(target);
        HookLog("[cdn] MH_EnableHook st=%d", (int)st2);
    }
}


// ===== CDN wrapper probe =====
using FnCdnWrap = uint64_t(*)(uint64_t, uint32_t);
static FnCdnWrap g_origCdnWrap = nullptr;
static volatile LONG g_cdnwHits = 0;
static uint64_t DetourCdnWrap(uint64_t obj, uint32_t arg)
{
    LONG n = InterlockedIncrement(&g_cdnwHits);
    HookLog("[cdnw] ENTER hit#%ld rcx=%llX rdx=%u", n,
            (unsigned long long)obj, arg);
    if (n <= 20 && obj > 0x10000) {
        __try { DumpCdnObject(obj); }
        __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[cdnw] dump EXC"); }
    }
    __try {
        uint64_t r = g_origCdnWrap(obj, arg);
        HookLog("[cdnw] RETURN hit#%ld r=%llX", n, (unsigned long long)r);
        return r;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        HookLog("[cdnw] CALL EXC hit#%ld code=%08X", n, GetExceptionCode());
        return 0;
    }
}
static void RegisterCdnWrapProbe()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) { HookLog("[cdnw] Weixin.dll null"); return; }
    void* target = (void*)((uintptr_t)wx + 0x22fb320);
    MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourCdnWrap, (LPVOID*)&g_origCdnWrap);
    HookLog("[cdnw] MH_CreateHook(%p) st=%d orig=%p", target, (int)st, (void*)g_origCdnWrap);
    if (st == MH_OK) HookLog("[cdnw] MH_EnableHook st=%d", (int)MH_EnableHook(target));
}


// ===== CDN candidate entry probes =====
using FnCand5 = uint64_t(*)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
#define DEF_CDN_CAND(NAME, RVA) \
    static FnCand5 g_orig_##NAME = nullptr; \
    static volatile LONG g_##NAME##_hits = 0; \
    static uint64_t Detour_##NAME(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5) { \
        LONG n = InterlockedIncrement(&g_##NAME##_hits); \
        HookLog("[cand:" #NAME "] ENTER hit#%ld a1=%llX a2=%llX a3=%llX a4=%llX", n, \
                (unsigned long long)a1, (unsigned long long)a2, \
                (unsigned long long)a3, (unsigned long long)a4); \
        if (n <= 20 && a1 > 0x10000) { \
            __try { DumpCdnObject(a1); } \
            __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[cand:" #NAME "] dump EXC"); } \
        } \
        __try { \
            uint64_t r = g_orig_##NAME(a1, a2, a3, a4, a5); \
            HookLog("[cand:" #NAME "] RETURN hit#%ld r=%llX", n, (unsigned long long)r); \
            return r; \
        } __except (EXCEPTION_EXECUTE_HANDLER) { \
            HookLog("[cand:" #NAME "] CALL EXC hit#%ld code=%08X", n, GetExceptionCode()); \
            return 0; \
        } \
    } \
    static void Reg_##NAME() { \
        HMODULE wx = GetModuleHandleW(L"Weixin.dll"); \
        if (!wx) return; \
        void* target = (void*)((uintptr_t)wx + RVA); \
        MH_STATUS st = MH_CreateHook(target, (LPVOID)&Detour_##NAME, (LPVOID*)&g_orig_##NAME); \
        HookLog("[cand:" #NAME "] create st=%d target=%p", (int)st, target); \
        if (st == MH_OK) HookLog("[cand:" #NAME "] enable st=%d", (int)MH_EnableHook(target)); \
    }
DEF_CDN_CAND(sns, 0x29b5160)
DEF_CDN_CAND(https1, 0x29b5570)
DEF_CDN_CAND(https2, 0x214d8a0)
static void RegisterCdnCandidates()
{
    Reg_sns();
    Reg_https1();
    Reg_https2();
}


// ===== CDN task-create candidate probes =====
using FnCreate = void(*)(uint64_t, uint64_t);
#define DEF_CREATE(NAME, RVA) \
    static FnCreate g_orig_##NAME = nullptr; \
    static volatile LONG g_##NAME##_hits = 0; \
    static void Detour_##NAME(uint64_t a1, uint64_t a2) { \
        LONG n = InterlockedIncrement(&g_##NAME##_hits); \
        { void** _ra = (void**)_AddressOfReturnAddress(); void* _rc = nullptr; \
          uintptr_t _wb = (uintptr_t)GetModuleHandleW(L"Weixin.dll"); \
          __try { _rc = *_ra; } __except (EXCEPTION_EXECUTE_HANDLER) { _rc = nullptr; } \
          HookLog("[create:" #NAME "] ENTER hit#%ld a1=%llX a2=%llX ret_slot=%p ret_code=%p", n, \
                  (unsigned long long)a1, (unsigned long long)a2, _ra, _rc); \
          for (int _si = 0; _si < 32; ++_si) { \
            void* _sv = nullptr; \
            __try { _sv = _ra[_si]; } __except (EXCEPTION_EXECUTE_HANDLER) { break; } \
            if ((uintptr_t)_sv >= _wb && (uintptr_t)_sv < _wb + 0xC000000ULL) \
              HookLog("[create:" #NAME "] STACK[%d] rva=%llX", _si, (unsigned long long)((uintptr_t)_sv - _wb)); \
          } } \
        if (n <= 20) { \
            __try { if (a1 > 0x10000) DumpCdnObject(a1); } \
            __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[create:" #NAME "] dump1 EXC"); } \
            __try { if (a2 > 0x10000) DumpCdnObject(a2); } \
            __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[create:" #NAME "] dump2 EXC"); } \
        } \
        __try { \
            g_orig_##NAME(a1, a2); \
            HookLog("[create:" #NAME "] RETURN hit#%ld", n); \
        } __except (EXCEPTION_EXECUTE_HANDLER) { \
            HookLog("[create:" #NAME "] CALL EXC hit#%ld code=%08X", n, GetExceptionCode()); \
        } \
    } \
    static void Reg_##NAME() { \
        HMODULE wx = GetModuleHandleW(L"Weixin.dll"); \
        if (!wx) return; \
        void* target = (void*)((uintptr_t)wx + RVA); \
        MH_STATUS st = MH_CreateHook(target, (LPVOID)&Detour_##NAME, (LPVOID*)&g_orig_##NAME); \
        HookLog("[create:" #NAME "] create st=%d target=%p", (int)st, target); \
        if (st == MH_OK) HookLog("[create:" #NAME "] enable st=%d", (int)MH_EnableHook(target)); \
    }
DEF_CREATE(createA, 0x1682f80)
DEF_CREATE(createB, 0x1685c60)
static void RegisterCreateProbes()
{
    Reg_createA();
    Reg_createB();
}


// ===== CDN request crypto probe (XXTEA/TEA) =====
using FnTea = uint64_t(*)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
static FnTea g_origTea = nullptr;
static volatile LONG g_teaHits = 0;
static uint64_t DetourTea(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
{
    LONG n = InterlockedIncrement(&g_teaHits);
    uintptr_t wb = (uintptr_t)GetModuleHandleW(L"Weixin.dll");
    void** ra = (void**)_AddressOfReturnAddress();
    void* rc = nullptr;
    __try { rc = *ra; } __except (EXCEPTION_EXECUTE_HANDLER) { rc = nullptr; }
    uintptr_t crva = ((uintptr_t)rc >= wb) ? ((uintptr_t)rc - wb) : 0;
    bool want = (crva >= 0x166d420ULL && crva < 0x166f800ULL) ||
                (crva >= 0x3b56a0ULL && crva < 0x3b5900ULL);
    if (want) {
        HookLog("[tea] CDN hit#%ld caller_rva=%llX len=%llu p1=%llX p2=%llX",
                n, (unsigned long long)crva, (unsigned long long)a3,
                (unsigned long long)a1, (unsigned long long)a2);
        if (a2 > 0x10000 && a3 > 0 && a3 <= 4096) {
            __try {
                unsigned char* p = (unsigned char*)a2;
                char hex[4096] = {0};
                size_t lim = (a3 < 512) ? (size_t)a3 : 512;
                for (size_t i = 0; i < lim; ++i) sprintf_s(hex + i*2, 3, "%02X", p[i]);
                HookLog("[tea] CDN input=%s", hex);
            } __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[tea] CDN input EXC"); }
        }
    }
    __try {
        uint64_t r = g_origTea(a1, a2, a3, a4, a5);
        if (want) HookLog("[tea] CDN RETURN#%ld r=%llX", n, (unsigned long long)r);
        return r;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        HookLog("[tea] CDN CALL EXC#%ld code=%08X", n, GetExceptionCode());
        return 0;
    }
}

static void RegisterTeaProbe()
{
    HMODULE wx = GetModuleHandleW(L"Weixin.dll");
    if (!wx) return;
    void* target = (void*)((uintptr_t)wx + 0x1618470);
    MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourTea, (LPVOID*)&g_origTea);
    HookLog("[tea] create st=%d target=%p", (int)st, target);
    if (st == MH_OK) HookLog("[tea] enable st=%d", (int)MH_EnableHook(target));
}

// 延迟初始化线程：等 weixin.exe / Weixin.dll 就绪后再初始化
static DWORD WINAPI DeferredInitThread(LPVOID)
{
    AddVectoredExceptionHandler(1, CrashLogger);
    HookLog("[init] thread start pid=%lu", GetCurrentProcessId());

    HMODULE exe = nullptr;
    for (int i = 0; i < 300 && !exe; ++i) { exe = GetModuleHandleW(L"weixin.exe"); if (!exe) Sleep(200); }
    if (!exe) { HookLog("[init] weixin.exe not found, abort"); return 0; }
    HookLog("[init] weixin.exe=%p", exe);

    HMODULE wx = nullptr;
    for (int i = 0; i < 600 && !wx; ++i) { wx = GetModuleHandleW(L"Weixin.dll"); if (!wx) Sleep(200); }
    if (!wx) { HookLog("[init] Weixin.dll not found after 120s, abort"); return 0; }
    HookLog("[init] Weixin.dll=%p", wx);

    Sleep(1500);
    HookLog("[init] calling CustomInit");
    CustomInit(g_hSelfModule);
    RegisterCreateProbes();
    RegisterTeaProbe();
    RegisterCdnProbe();
    RegisterCdnWrapProbe();
    HookLog("[init] CustomInit returned g_httpServer=%p", (void*)g_httpServer);
    g_IsLogin = 1;
    HookLog("[init] g_IsLogin set to 1");
    return 0;
}
BOOL APIENTRY DllMain(
    HMODULE hModule,
    DWORD  ul_reason_for_call,
    LPVOID lpReserved
)
{
    if (ul_reason_for_call == DLL_PROCESS_ATTACH)
    {
        g_hModule = hModule;  // 保存模块基址
        g_hSelfModule = hModule;
        HookLog("[dllmain] attach module=%p", hModule);

        DisableThreadLibraryCalls(hModule);

        // 先加载和初始化真实 version.dll
        InitRealDll();

        if (!IsMainWeixinProcess())
        {
            // 子进程：直接卸载自己
            CreateThread(nullptr,0,SelfUnloadThread,hModule,0,nullptr);
            //OutputDebugStringA("[VxHook] not main process!\n");
            return TRUE;
        }
        //OutputDebugStringA("[VxHook] main process!\n");

        #ifdef HIDE_SELF_MODULE
        HideModuleFromPEB(hModule); // 隐藏本模块
#endif
#ifdef _DEBUG
		OutputDebugStringA("[VxHook] HideModuleFromPEB success!\n");
#endif

        // 主进程才执行：改为延迟初始化（等 Weixin.dll 加载完成）
        CreateThread(nullptr, 0, DeferredInitThread, nullptr, 0, nullptr);
    }
    return TRUE;
}

