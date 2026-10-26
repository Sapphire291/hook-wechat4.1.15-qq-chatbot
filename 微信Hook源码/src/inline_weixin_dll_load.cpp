#include <cstdint>
#include <cstdio>
#include <string>
#include <Windows.h>
#include <winternl.h>
#include <cstring>
#include <sstream>

#include "http_server.h"
#include "Hook_Method.h"

#include "global.h"
#include <MinHook.h>
#include "tools.h"
#include "HookManager.h"
#include "json.hpp"
#include "wx_ini_reader.h"
#include "inline_weixin_dll_load.h"
#include "hook_log.h"
#include <oleauto.h>
#pragma comment(lib, "oleaut32.lib")

namespace sendprobe {

    // ===== 全局状态（定义在此，供各 detour 使用）=====
    static volatile LONG64 g_cap_s5[5] = { 0,0,0,0,0 };
    static volatile LONG   g_cap_s5_ready = 0;
    static volatile LONG64 g_cap_mgr[2] = { 0,0 };
    static volatile LONG64 g_cap_mgr2[2] = { 0,0 };   // rdx, r9
    static HWND        g_wxHwnd = nullptr;
    static WNDPROC     g_origWndProc = nullptr;
    static volatile LONG g_pendingSend = 0;
    static uint64_t    g_sendArgs[4] = {0,0,0,0};
    static volatile LONG   g_cap_mgr_ready = 0;
    static volatile LONG   g_scan_done = 0;
    static void*           g_tg_s5 = nullptr;
    static void*           g_tg_mgr = nullptr;
    static volatile LONG   g_deepBudget = 0;
    static uint64_t        g_rootProv = 0;        // 主窗口根 provider（Simple 接口指针，UiaReturnRawElementProvider 捕获）
    static volatile LONG   g_accGateOn = 0;       // Qt 无障碍门控已激活
    static volatile LONG   g_capturing = 0;       // 正在主动抓取根 provider
    static volatile LONG   g_uiTaskPending = 0;   // 有 UI 线程任务待执行
    static volatile LONG   g_uiTaskDone = 0;      // UI 线程任务已完成
    static void          (*g_uiTaskFn)(void*) = nullptr;
    static void*           g_uiTaskArg = nullptr;

    // ===== 前置声明 =====
    static void CaptureFast(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static void CaptureMgrFast(uint64_t, uint64_t);
    static void DeepScan(const char*, uint64_t, int);
    static void DumpStrings(const char*, uint64_t, int); void Register(); void RegisterAllAsync();
    static bool ProvIsValidObj(uint64_t simple);
    static uint64_t ProvAddRefSimple(uint64_t simple);
}
#include "hook_xlog.h"



using json = nlohmann::json;

typedef NTSTATUS(NTAPI* PFN_NtQueryInformationProcess)(
    HANDLE,
    PROCESSINFOCLASS,
    PVOID,
    ULONG,
    PULONG
    );



DWORD GetParentProcessId()
{
    PFN_NtQueryInformationProcess NtQueryInformationProcess =
        (PFN_NtQueryInformationProcess)GetProcAddress(
            GetModuleHandleW(L"ntdll.dll"),
            "NtQueryInformationProcess"
        );

    if (!NtQueryInformationProcess)
        return 0;

    PROCESS_BASIC_INFORMATION pbi = { 0 };

    NTSTATUS status = NtQueryInformationProcess(
        GetCurrentProcess(),
        ProcessBasicInformation,
        &pbi,
        sizeof(pbi),
        nullptr
    );

    if (status != 0)
        return 0;

    return (DWORD)(ULONG_PTR)pbi.Reserved3;
}


DWORD WINAPI AfterLoginInitThread(LPVOID)
{
    // 等待登录成功
    while (g_IsLogin != 1)
    {
        Sleep(300);
    }

    return 0;
}

// 过低版本
void Patch_Low_Version()
{
    // XWECHAT_MAIN_CLAZZ_OFFSET 4.1.8.67
    DWORD_PTR baseAddress = (DWORD_PTR)g_hWeixinDll + reinterpret_cast<uintptr_t>(XWECHAT_MAIN_CLAZZ_OFFSET);

    DWORD_PTR* pPointer = (DWORD_PTR*)baseAddress;
    if (*pPointer == NULL) {
        // 处理空指针情况
        return;
    }

    DWORD_PTR targetAddress = (*pPointer) + 0xB8 + 0x90;        //80  = 00000000F2541843



    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 4, PAGE_EXECUTE_READWRITE, &oldProtect);

    *(pTarget) = 0x43;      // 低位字节 = 67
    *(pTarget + 1) = 0x18;
    *(pTarget + 2) = 0x54;  // 高位字节
    *(pTarget + 3) = 0xF2;  

    // 恢复保护属性
    VirtualProtect(pTarget, 4, oldProtect, &oldProtect);
}


void Patch_Low_Version_m2()
{
    // 4.1.8.67 addresses
    struct PatchInfo {
        DWORD_PTR addr;
        BYTE bytes[4];
    };

    // F2510201
    // F2541843
    PatchInfo patches[] = {
        { (DWORD_PTR)g_hWeixinExe + 0x36E2, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x18EB, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x1B0E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x204B, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE4C33, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x2AC9481, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x3248CE9, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0x379D66E, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinExe + 0x204FA0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xDE540C, { 0x43, 0x19, 0x6C, 0xF2 } }, 
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D0, { 0x43, 0x19, 0x6C, 0xF2 } },
        { (DWORD_PTR)g_hWeixinDll + 0xA3518D4, { 0x43, 0x19, 0x6C, 0xF2 } }
    };

    DWORD oldProtect;
    for (int i = 0; i < 3; i++) {
        VirtualProtect((LPVOID)patches[i].addr, 4, PAGE_EXECUTE_READWRITE, &oldProtect);
        memcpy((void*)patches[i].addr, patches[i].bytes, 4);
        VirtualProtect((LPVOID)patches[i].addr, 4, oldProtect, &oldProtect);
    }

}


//启用防撤回
void Patch_Revoke()
{
    // 计算目标地址
    DWORD_PTR targetAddress = (DWORD_PTR)g_hWeixinDll + g_Patch_Revoke;

    // 直接通过指针修改
    BYTE* pTarget = (BYTE*)targetAddress;

    // 修改内存保护属性
    DWORD oldProtect;
    VirtualProtect(pTarget, 2, PAGE_EXECUTE_READWRITE, &oldProtect);

    *pTarget = 0x90;           // 第一个字节改为 nop
    *(pTarget + 1) = 0xE9;     // 第二个字节改为 jmp 操作码

    // 恢复保护属性
    VirtualProtect(pTarget, 2, oldProtect, &oldProtect);
}


using FnWcProbe = void(__fastcall*)(void* out_buffer, void* out_len, void* input_param);
FnWcProbe g_Original = nullptr;





void Evt_WeixinLoad()
{
    HookLog("[Evt] enter");
    g_hWeixinDll = GetModuleHandleW(L"Weixin.dll");
    HookLog("[Evt] Weixin.dll=%p", g_hWeixinDll);
    if (!g_hWeixinDll) { HookLog("[Evt] Weixin.dll null, abort"); return; }

#ifdef _DEBUG
    char debugMsg[256];
    snprintf(debugMsg, sizeof(debugMsg),"[Evt_WeixinLoad] Weixin.dll: 0x%p\n",g_hWeixinDll);
    OutputDebugStringA(debugMsg);
#endif


    进程PID = GetCurrentProcessId();
    父进程PID = GetParentProcessId();

    //过低版本 4.1.8.67
    //Patch_Low_Version_m2();

    //get base DirPath
    //InitStandardPaths();
    // Patch_Revoke();  // 4.1.10.27 专用硬编码补丁，4.1.15 上会破坏代码，禁用


    // 创建并启动HTTP服务器
    if (!g_httpServer)
    {
        g_httpServer = new HttpServer();
        HookLog("[Evt] starting http on 0.0.0.0:%d", g_StartPort);
        


g_httpServer->Start("0.0.0.0", g_StartPort);
        HookLog("[Evt] http started");
    sendprobe::RegisterAllAsync();   // 延迟注册，避免初始化期死锁
    }
#ifdef _DEBUG
    //xLog 日志
    Hook_Call(WeixinDll_Offset(0x108678), 5, hook::MyCallHandler_xLog);
#endif
    

    
    //取回调URL
    GetWxRecvUrl();         
}

// ===================== 发送参数探针（MinHook 版，方案三） =====================
namespace sendprobe {
    typedef void(*FnSend5)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static FnSend5 g_origSend5 = nullptr;
    static volatile LONG g_hits = 0;

    static void DumpArg(const char* name, uint64_t p)
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        if (!p || p < 0x10000) { HookLog("[probe] %s=%llX (invalid)", name, (unsigned long long)p); return; }
        HookLog("[probe] %s=%llX", name, (unsigned long long)p);
        for (int i = 0; i < 10; ++i) {
            uint64_t v = 0;
            __try { v = *(uint64_t*)(p + i * 8); }
            __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[probe]   +%02X <unreadable>", i * 8); break; }
            if (v >= base && v < base + 0x10000000ULL)
                HookLog("[probe]   +%02X = %llX (mod)", i * 8, (unsigned long long)(v - base));
            else
                HookLog("[probe]   +%02X = %llX", i * 8, (unsigned long long)v);
        }
        char buf[49] = { 0 };
        __try { memcpy(buf, (void*)p, 48); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return; }
        bool printable = true; int n = 0;
        for (; n < 48; ++n) {
            if (buf[n] == 0) break;
            unsigned char ch = (unsigned char)buf[n];
            if (ch < 32 || ch > 126) { printable = false; break; }
        }
        if (printable && n >= 2) { buf[n] = 0; HookLog("[probe]   str@ptr = %s", buf); }
    }

    // 顺着指针找可打印字符串，定位文本/wxid 所在
    static void DumpStrings(const char* tag, uint64_t p, int depth)
    {
        if (!p || p < 0x10000 || depth > 2) return;
        static unsigned char buf[4096];
        memset(buf, 0, sizeof(buf));
        __try { memcpy(buf, (void*)p, 4096); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return; }
        int i = 0;
        while (i < 4096) {
            if (buf[i] >= 32 && buf[i] < 127) {
                int j = i;
                while (j < 4096 && buf[j] >= 32 && buf[j] < 127) j++;
                if (j - i >= 4) {
                    char s[160] = { 0 };
                    int n = j - i; if (n > 120) n = 120;
                    memcpy(s, buf + i, n); s[n] = 0;
                    HookLog("[probe] str[%s+%03X] = %s", tag, i, s);
                }
                i = j;
            } else i++;
        }
    }
    // 栈回溯：扫描调用栈里的返回地址（返回地址前 5 字节须为 E8）
    static void WalkStack(const char* tag)
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uintptr_t lo = base + 0x1000;
        uintptr_t hi = base + 0xC000000;
        uint64_t* p = (uint64_t*)_AddressOfReturnAddress();
        int logged = 0;
        HookLog("[stack] %s begin", tag);
        for (int i = 0; i < 0x200 && logged < 24; ++i) {
            uint64_t v = 0;
            __try { v = p[i]; } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            if (v < lo || v >= hi) continue;
            unsigned char prev = 0;
            __try { prev = *(unsigned char*)(v - 5); } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }
            if (prev != 0xE8) continue;
            int32_t rel = 0;
            __try { rel = *(int32_t*)(v - 4); } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }
            uint64_t target = (v - 5) + 5 + (int64_t)rel;
            HookLog("[stack] #%d ret_rva=%llX call_rva=%llX target_rva=%llX",
                    logged, (unsigned long long)(v - base), (unsigned long long)((v - 5) - base),
                    (unsigned long long)((target >= base) ? (target - base) : 0));
            logged++;
        }
        HookLog("[stack] %s end", tag);
    }
    // ============ 方案A：快照 + 重放 ============
    static const size_t SNAP_PRE   = 0x200;
    static const size_t SNAP_SIZE  = 0x1800;
    static unsigned char g_snap[SNAP_SIZE];
    static bool  g_hasSnap = false;
    static uint64_t g_snapA1 = 0;
    static uint64_t g_snapArgs[5] = {0,0,0,0,0};
    // 由探针实测得到的固定偏移
    static const size_t OFF_TEXT = 0x438;
    static const size_t OFF_WXID1 = 0x510;
    static const size_t OFF_WXID2 = 0x598;

    // 写 WeixinString（inline_buf[16] / len / cap）
    static void WriteWxString(unsigned char* base, size_t off, const char* s)
    {
        size_t n = strlen(s);
        unsigned char* p = base + off;
        if (n <= 15) {
            memset(p, 0, 16);
            memcpy(p, s, n);
            *(uint64_t*)(p + 0x10) = (uint64_t)n;
            *(uint64_t*)(p + 0x18) = 15;
        } else {
            // 超长文本：分配堆缓冲并设置 ptr/len/cap
            char* heap = (char*)HeapAlloc(GetProcessHeap(), 0, n + 1);
            if (!heap) return;
            memcpy(heap, s, n); heap[n] = 0;
            *(uint64_t*)(p + 0x00) = (uint64_t)heap;
            *(uint64_t*)(p + 0x10) = (uint64_t)n;
            *(uint64_t*)(p + 0x18) = (uint64_t)n;
        }
    }

    static void CaptureSnapshot(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        if (g_hasSnap) return;
        uint64_t base = a1 - SNAP_PRE;
        __try { memcpy(g_snap, (void*)base, SNAP_SIZE); }
        __except (EXCEPTION_EXECUTE_HANDLER) { HookLog("[snap] copy failed"); return; }
        g_snapA1 = a1;
        g_snapArgs[0] = a1; g_snapArgs[1] = a2; g_snapArgs[2] = a3; g_snapArgs[3] = a4; g_snapArgs[4] = a5;
        g_hasSnap = true;
        HookLog("[snap] captured base=%llX a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX",
                (unsigned long long)base, (unsigned long long)a1, (unsigned long long)a2,
                (unsigned long long)a3, (unsigned long long)a4, (unsigned long long)a5);
        char txt[64] = {0};
        __try { memcpy(txt, (void*)(a1 + OFF_TEXT), 48); } __except (EXCEPTION_EXECUTE_HANDLER) {}
        HookLog("[snap] text@+438 = %.48s", txt);
    }

    // 导出给 HTTP 路由调用
    extern "C" __declspec(dllexport) int WxReplaySendText(const char* wxid, const char* text)
    {
        if (!g_hasSnap || !g_origSend5) { HookLog("[replay] no snapshot/orig"); return -1; }
        unsigned char* buf = (unsigned char*)HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, SNAP_SIZE);
        if (!buf) return -2;
        memcpy(buf, g_snap, SNAP_SIZE);
        uint64_t base = g_snapA1 - SNAP_PRE;
        // 关键：修正结构内部指向"原栈"的自指针，改为指向新 buf
        {
            uint64_t lo = base, hi = base + SNAP_SIZE, fixed = 0;
            for (size_t off = 0; off + 8 <= SNAP_SIZE; off += 8) {
                uint64_t v = *(uint64_t*)(buf + off);
                if (v >= lo && v < hi) { *(uint64_t*)(buf + off) = (uint64_t)(buf + (v - lo)); fixed++; }
            }
            HookLog("[replay] self-pointer fixup: %llu", (unsigned long long)fixed);
        }
        unsigned char* a1 = buf + (g_snapArgs[0] - base);
        unsigned char* a2 = buf + (g_snapArgs[1] - base);
        unsigned char* a3 = buf + (g_snapArgs[2] - base);
        unsigned char* a4 = buf + (g_snapArgs[3] - base);
        unsigned char* a5 = buf + (g_snapArgs[4] - base);
        WriteWxString(a1, OFF_TEXT, text);
        WriteWxString(a1, OFF_WXID1, wxid);
        WriteWxString(a1, OFF_WXID2, wxid);
        HookLog("[replay] send wxid=%s text=%s (len=%d)", wxid, text, (int)strlen(text));
        unsigned int exCode = 0; uint64_t exAddr = 0;
        __try {
            g_origSend5((uint64_t)a1, (uint64_t)a2, (uint64_t)a3, (uint64_t)a4, (uint64_t)a5);
            HookLog("[replay] returned normally");
        }
        __except (exCode = GetExceptionCode(), exAddr = (uint64_t)GetExceptionInformation()->ExceptionRecord->ExceptionAddress, EXCEPTION_EXECUTE_HANDLER) {
            HookLog("[replay] EXCEPTION code=%08X addr=%llX", exCode, (unsigned long long)exAddr);
            HeapFree(GetProcessHeap(), 0, buf);
            return -3;
        }
        HeapFree(GetProcessHeap(), 0, buf);
        return 0;
    }
    static void DetourSend5(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        // 热路径：只捕获一次指针，无任何 IO / 扫描（避免微信卡死）
        if (!g_cap_s5_ready && InterlockedCompareExchange(&g_cap_s5_ready, 1, 0) == 0) {
            g_cap_s5[0] = (LONG64)a1; g_cap_s5[1] = (LONG64)a2; g_cap_s5[2] = (LONG64)a3;
            g_cap_s5[3] = (LONG64)a4; g_cap_s5[4] = (LONG64)a5;
        }
        g_origSend5(a1, a2, a3, a4, a5);
    }

    // 上一层函数探针：0x3DE9600（其内部调用 0x19E5060）
    typedef void(*FnTop5)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static FnTop5 g_origTop5 = nullptr;
    static volatile LONG g_topHits = 0;

    static void DetourTop5(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        LONG n = InterlockedIncrement(&g_topHits);
        if (n <= 4) {
            uintptr_t base = (uintptr_t)g_hWeixinDll;
            uint64_t ret = (uint64_t)_ReturnAddress();
            HookLog("[top] hit#%ld caller=%llX a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX",
                    n, (unsigned long long)((ret >= base && ret < base + 0x10000000ULL) ? ret - base : ret),
                    (unsigned long long)a1, (unsigned long long)a2, (unsigned long long)a3,
                    (unsigned long long)a4, (unsigned long long)a5);
            DumpStrings("top-a1", a1, 0); DumpStrings("top-a2", a2, 0);
            DumpStrings("top-a3", a3, 0); DumpStrings("top-a4", a4, 0);
            DumpStrings("top-a5", a5, 0);
        }
        g_origTop5(a1, a2, a3, a4, a5);
    }

    // 更上一层探针：0x3DE7800（虚方法，文本进入点候选）
    typedef void(*FnUp5)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static FnUp5 g_origUp5 = nullptr;
    static volatile LONG g_upHits = 0;

    static void DetourUp5(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        LONG n = InterlockedIncrement(&g_upHits);
        if (n <= 3) {
            HookLog("[up] hit#%ld rcx=%llX rdx=%llX r8=%llX r9=%llX",
                    n, (unsigned long long)a1, (unsigned long long)a2,
                    (unsigned long long)a3, (unsigned long long)a4);
            DumpArg("up-rcx", a1);
            DumpArg("up-rdx", a2);
            DumpStrings("up-rcx", a1, 0);
            uint64_t p8 = 0;
            __try { p8 = *(uint64_t*)(a1 + 8); } __except (EXCEPTION_EXECUTE_HANDLER) {}
            if (p8 > 0x10000) { HookLog("[up] rcx+8=%llX", (unsigned long long)p8); DumpArg("up-rcx8", p8); DumpStrings("up-rcx8", p8, 0); }
        }
        g_origUp5(a1, a2, a3, a4, a5);
    }

    // 对象图钻取：沿指针向下 2 层，输出可打印字符串（带上限，避免日志爆炸）
    static void DeepScan(const char* tag, uint64_t p, int depth)
    {
        if (!p || p < 0x10000 || depth > 2) return;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        if (p >= base && p < base + 0xC000000ULL) return;   // 模块内跳过
        if (InterlockedIncrement(&g_deepBudget) > 150) return;
        DumpStrings(tag, p, 0);
        if (depth >= 2) return;
        for (int k = 0; k < 24; ++k) {
            uint64_t v = 0;
            __try { v = *(uint64_t*)(p + k * 8); } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            if (v < 0x10000) continue;
            if (v >= base && v < base + 0xC000000ULL) continue;
            DeepScan(tag, v, depth + 1);
        }
    }
    // 多层探针：定位文本从哪一层进入
    #define DEF_UP_PROBE(NAME, RVA)                                                          \
    static FnUp5 g_orig_##NAME = nullptr;                                               \
    static void Detour_##NAME(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5) \
    {                                                                                   \
        if (!g_cap_mgr_ready && InterlockedCompareExchange(&g_cap_mgr_ready, 1, 0) == 0) { \
            g_cap_mgr[0] = (LONG64)a1; g_cap_mgr[1] = (LONG64)a3;                         \
            g_cap_mgr2[0] = (LONG64)a2; g_cap_mgr2[1] = (LONG64)a4;                        \
        }                                                                               \
        g_orig_##NAME(a1, a2, a3, a4, a5);                                              \
    }                                                                                   \
    static void Reg_##NAME()                                                            \
    {                                                                                   \
        uintptr_t base = (uintptr_t)g_hWeixinDll;                                       \
        void* tg = (void*)(base + RVA);                                                 \
        MH_STATUS st = MH_CreateHook(tg, (LPVOID)&Detour_##NAME, (LPVOID*)&g_orig_##NAME); \
        HookLog("[hookreg] " #NAME " st=%d", (int)st);                                   \
        if (st == MH_OK) MH_EnableHook(tg);                                             \
    }

    DEF_UP_PROBE(fn1C11050, 0x1C11050)
    DEF_UP_PROBE(fn3DE3B00, 0x3DE3B00)
    DEF_UP_PROBE(fn3DE5D90, 0x3DE5D90)
    DEF_UP_PROBE(fn3DE7800, 0x3DE7800)
    void RegisterUp()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* target = (void*)(base + 0x3DE7800);
        MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourUp5, (LPVOID*)&g_origUp5);
        HookLog("[up] MH_CreateHook(0x3DE7800) st=%d", (int)st);
        if (st == MH_OK) HookLog("[up] MH_EnableHook st=%d", (int)MH_EnableHook(target));
    }
    void RegisterTop()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* target = (void*)(base + 0x3DE9600);
        MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourTop5, (LPVOID*)&g_origTop5);
        HookLog("[top] MH_CreateHook(0x3DE9600) st=%d orig=%p", (int)st, (void*)g_origTop5);
        if (st == MH_OK) { HookLog("[top] MH_EnableHook st=%d", (int)MH_EnableHook(target)); }
    }
    void Register()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* target = (void*)(base + 0x19E5060);
        g_tg_s5 = target;
        MH_STATUS st = MH_CreateHook(target, (LPVOID)&DetourSend5, (LPVOID*)&g_origSend5);
        HookLog("[probe] MH_CreateHook(0x19E5060) st=%d orig=%p", (int)st, (void*)g_origSend5);
        if (st == MH_OK) {
            MH_STATUS st2 = MH_EnableHook(target);
            HookLog("[probe] MH_EnableHook st=%d", (int)st2);
        }
    }
    // ======== 低侵入方案：热路径只存指针，扫描放到独立线程 ========

    // 热路径：只做一次指针捕获（无 IO，无扫描）
    static void CaptureFast(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        if (g_cap_s5_ready) return;
        if (InterlockedCompareExchange(&g_cap_s5_ready, 1, 0) == 0) {
            g_cap_s5[0] = (LONG64)a1; g_cap_s5[1] = (LONG64)a2; g_cap_s5[2] = (LONG64)a3;
            g_cap_s5[3] = (LONG64)a4; g_cap_s5[4] = (LONG64)a5;
        }
    }
    static void CaptureMgrFast(uint64_t rcx, uint64_t r8)
    {
        if (g_cap_mgr_ready) return;
        if (InterlockedCompareExchange(&g_cap_mgr_ready, 1, 0) == 0) {
            g_cap_mgr[0] = (LONG64)rcx; g_cap_mgr[1] = (LONG64)r8;
        }
    }

    // 独立扫描线程：发送结束后再做重活，避免影响微信
    static void DumpQwords(const char* tag, uint64_t p, int count)
    {
        if (!p || p < 0x10000) return;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        for (int i = 0; i < count; ++i) {
            uint64_t v = 0;
            __try { v = *(uint64_t*)(p + i * 8); } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            if (v >= base && v < base + 0xC000000ULL)
                HookLog("[layout] %s+%03X = %llX (mod+%llX)", tag, i * 8,
                        (unsigned long long)v, (unsigned long long)(v - base));
            else
                HookLog("[layout] %s+%03X = %llX", tag, i * 8, (unsigned long long)v);
        }
    }
    static DWORD WINAPI ScanThread(LPVOID)
    {
        for (int i = 0; i < 120 && !(g_cap_s5_ready && g_cap_mgr_ready); ++i) Sleep(500);
        if (!g_cap_s5_ready) { HookLog("[scan] no capture, abort"); g_scan_done = 1; return 0; }
        Sleep(3000);   // 等这次发送彻底结束
        HookLog("[scan] begin (off hot path)");
        HookLog("[scan] s5 a1=%llX a2=%llX a3=%llX a4=%llX a5=%llX",
                (unsigned long long)g_cap_s5[0], (unsigned long long)g_cap_s5[1],
                (unsigned long long)g_cap_s5[2], (unsigned long long)g_cap_s5[3],
                (unsigned long long)g_cap_s5[4]);
        g_deepBudget = 0;
        DeepScan("scan_a1", (uint64_t)g_cap_s5[0], 0);
        if (g_cap_mgr_ready) {
            HookLog("[scan] mgr rcx=%llX r8=%llX",
                    (unsigned long long)g_cap_mgr[0], (unsigned long long)g_cap_mgr[1]);
            g_deepBudget = 0;
            DeepScan("scan_mgr", (uint64_t)g_cap_mgr[0], 0);
            g_deepBudget = 0;
            DeepScan("scan_mgr_r8", (uint64_t)g_cap_mgr[1], 0);
        }
        {
            uint64_t r8 = (uint64_t)g_cap_mgr[1];
            HookLog("[layout] === r8 object === r8=%llX rcx=%llX rdx=%llX r9=%llX",
                    (unsigned long long)r8, (unsigned long long)g_cap_mgr[0],
                    (unsigned long long)g_cap_mgr2[0], (unsigned long long)g_cap_mgr2[1]);
            DumpQwords("r8", r8, 12);
            DumpQwords("r8_wxid", r8 + 0x560, 10);       // 0x560~0x5A8
            DumpQwords("r8_text", r8 + 0x600, 12);       // 0x600~0x660
            DumpQwords("r8_text2", r8 + 0xD50, 12);      // 0xD50~0xDB0
            char* s1 = (char*)(r8 + 0x628);
            char* s2 = (char*)(r8 + 0x578);
            char* s3 = (char*)(r8 + 0xD78);
            HookLog("[layout] text@628=%.32s", s1);
            HookLog("[layout] wxid@578=%.32s", s2);
            HookLog("[layout] text2@D78=%.32s", s3);
        }
        HookLog("[scan] done");
        g_scan_done = 1;
        // 扫完立即卸载 hook，恢复微信原状
        return 0;
    }
    // 转储宽字符(UTF-16)字符串，用于在 r9 区域找输入文本
    static void DumpWideStrings(const char* tag, uint64_t p, int bytes)
    {
        if (!p || bytes <= 0 || bytes > 1024) return;
        static unsigned char buf[1024];
        memset(buf, 0, sizeof(buf));
        __try { memcpy(buf, (void*)p, (size_t)bytes); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return; }
        int i = 0;
        while (i + 1 < bytes) {
            unsigned short c = *(unsigned short*)(buf + i);
            if (c >= 32 && c < 127) {
                int j = i; char s[256] = {0}; int k = 0;
                while (j + 1 < bytes) {
                    unsigned short cc = *(unsigned short*)(buf + j);
                    if (cc < 32 || cc >= 127) break;
                    if (k < 200) s[k++] = (char)cc;
                    j += 2;
                }
                if (k >= 3) HookLog("[wstr] %s+%03X = %s", tag, i, s);
                i = j;
            } else if (c == 0) {
                i += 2;
            } else {
                i += 2;
            }
        }
    }
    // 极轻量调用者探针：只记录谁调用了 0x1C11050（每条 1 行，最多 3 条）
    typedef void(*FnCaller5)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static FnCaller5 g_origCaller = nullptr;
    static volatile LONG g_callerHits = 0;

    static void DetourCaller(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        LONG n = InterlockedIncrement(&g_callerHits);
        if (n <= 3) {
            uintptr_t base = (uintptr_t)g_hWeixinDll;
            uint64_t ret = (uint64_t)_ReturnAddress();
            HookLog("[caller] hit#%ld caller_rva=%llX rcx=%llX rdx=%llX r8=%llX r9=%llX",
                    n,
                    (unsigned long long)((ret >= base && ret < base + 0x10000000ULL) ? ret - base : ret),
                    (unsigned long long)a1, (unsigned long long)a2,
                    (unsigned long long)a3, (unsigned long long)a4);
            // 现场诊断：对象是否稳定、类型（虚表）是什么
            uint64_t vt1 = 0, vt3 = 0;
            __try { vt1 = *(uint64_t*)a1; } __except (EXCEPTION_EXECUTE_HANDLER) {}
            __try { if (a3 > 0x10000) vt3 = *(uint64_t*)a3; } __except (EXCEPTION_EXECUTE_HANDLER) {}
            if (g_sendArgs[0] == 0) { g_sendArgs[0] = a1; g_sendArgs[1] = a2; g_sendArgs[2] = a3; g_sendArgs[3] = a4; }
            HookLog("[caller]   rcxVt=%llX r8Vt=%llX (rva: %llX / %llX)",
                    (unsigned long long)vt1, (unsigned long long)vt3,
                    (unsigned long long)((vt1 >= base && vt1 < base + 0x10000000ULL) ? vt1 - base : 0),
                    (unsigned long long)((vt3 >= base && vt3 < base + 0x10000000ULL) ? vt3 - base : 0));
            if (n <= 2) {
                // 跟随 r9 区域的指针往下追（depth 1），寻找待发文本
                {
                    uintptr_t wbase = (uintptr_t)g_hWeixinDll;
                    for (int q = -16; q < 24; ++q) {
                        uint64_t pv = 0;
                        __try { pv = *(uint64_t*)(a4 + q * 8); } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }
                        if (pv < 0x10000) continue;
                        if (pv >= wbase && pv < wbase + 0x10000000ULL) continue;
                        HookLog("[r9ptr] r9%+d = %llX", q * 8, (unsigned long long)pv);
                        DumpStrings("p", pv, 0);
                        DumpWideStrings("wp", pv, 0x400);
                        uint64_t pv2 = 0;
                        __try { pv2 = *(uint64_t*)pv; } __except (EXCEPTION_EXECUTE_HANDLER) { continue; }
                        if (pv2 >= 0x10000 && !(pv2 >= wbase && pv2 < wbase + 0x10000000ULL)) {
                            HookLog("[r9ptr]   +0 = %llX", (unsigned long long)pv2);
                            DumpStrings("p2", pv2, 0);
                            DumpWideStrings("wp2", pv2, 0x400);
                        }
                    }
                }
                DumpStrings("r9-200", a4 - 0x200, 0);
                DumpStrings("r9", a4, 0);
                DumpStrings("r9+200", a4 + 0x200, 0);
                DumpWideStrings("w-r9-200", a4 - 0x200, 0x400);
                DumpWideStrings("w-r9", a4, 0x400);
                DumpWideStrings("w-r9+200", a4 + 0x200, 0x400);
                for (int k = 0; k < 6; ++k) {
                    uint64_t v = 0;
                    __try { v = *(uint64_t*)(a3 + k * 8); } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
                    HookLog("[caller]   r8+%02X = %llX", k * 8, (unsigned long long)v);
                }
            }
        }
        g_origCaller(a1, a2, a3, a4, a5);
    }

    static void RegisterCallerProbe()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* tg = (void*)(base + 0x1C11050);
        MH_STATUS st = MH_CreateHook(tg, (LPVOID)&DetourCaller, (LPVOID*)&g_origCaller);
        HookLog("[caller] hook st=%d", (int)st);
        if (st == MH_OK) HookLog("[caller] enable st=%d", (int)MH_EnableHook(tg));
    }
    // 回车发送处理函数探针：0x2A1570，只记录参数（每命中 1 行，最多 3 行）
    typedef void(*FnEnter)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
    static FnEnter g_origEnter = nullptr;
    static volatile LONG g_enterHits = 0;

    static void DetourEnter(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4, uint64_t a5)
    {
        LONG n = InterlockedIncrement(&g_enterHits);
        if (n <= 3) {
            uintptr_t base = (uintptr_t)g_hWeixinDll;
            uint64_t ret = (uint64_t)_ReturnAddress();
            HookLog("[enter] hit#%ld caller_rva=%llX rcx=%llX rdx=%llX r8=%llX r9=%llX",
                    n,
                    (unsigned long long)((ret >= base && ret < base + 0x10000000ULL) ? ret - base : ret),
                    (unsigned long long)a1, (unsigned long long)a2,
                    (unsigned long long)a3, (unsigned long long)a4);
        }
        g_origEnter(a1, a2, a3, a4, a5);
    }

    // 受控调用：用捕获的真实参数调用 0x1C11050（管理器发送入口）
    static int CallMgrImpl(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
    {
        if (!g_origCaller) return -1;
        unsigned int code = 0;
        __try { g_origCaller(a1, a2, a3, a4, 0); }
        __except (code = GetExceptionCode(), EXCEPTION_EXECUTE_HANDLER) {
            HookLog("[callmgr] EXCEPTION code=%08X", code);
            return -3;
        }
        return 0;
    }
    static void RegisterEnterProbe()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* tg = (void*)(base + 0x2A09F0);
        MH_STATUS st = MH_CreateHook(tg, (LPVOID)&DetourEnter, (LPVOID*)&g_origEnter);
        HookLog("[enter] hook st=%d", (int)st);
        if (st == MH_OK) HookLog("[enter] enable st=%d", (int)MH_EnableHook(tg));
    }
    // ===== 把 UI 层调用投递到微信 UI 主线程执行 =====

    static LRESULT CALLBACK DetourWndProc(HWND h, UINT msg, WPARAM w, LPARAM l)
    {
        if (InterlockedCompareExchange(&g_uiTaskPending, 0, 1) == 1) {
            if (g_uiTaskFn) g_uiTaskFn(g_uiTaskArg);
            InterlockedExchange(&g_uiTaskDone, 1);
        }
        if (g_pendingSend && InterlockedCompareExchange(&g_pendingSend, 0, 1) == 1) {
            if (g_sendArgs[0] && g_origCaller) {
                unsigned int code = 0;
                HookLog("[dispatch] exec on UI thread args=%llX/%llX/%llX/%llX",
                        (unsigned long long)g_sendArgs[0], (unsigned long long)g_sendArgs[1],
                        (unsigned long long)g_sendArgs[2], (unsigned long long)g_sendArgs[3]);
                __try {
                    g_origCaller(g_sendArgs[0], g_sendArgs[1], g_sendArgs[2], g_sendArgs[3], 0);
                    HookLog("[dispatch] exec done");
                }
                __except (code = GetExceptionCode(), EXCEPTION_EXECUTE_HANDLER) {
                    HookLog("[dispatch] EXCEPTION %08X", code);
                }
            }
        }
        return CallWindowProcW(g_origWndProc, h, msg, w, l);
    }

    static void RegisterWndProcHook()
    {
        g_wxHwnd = FindWindowW(nullptr, L"微信");
        if (!g_wxHwnd) g_wxHwnd = FindWindowW(L"Qt51514QWindowIcon", nullptr);
        HookLog("[wnd] hwnd=%p", (void*)g_wxHwnd);
        if (!g_wxHwnd) return;
        WNDPROC proc = (WNDPROC)GetWindowLongPtrW(g_wxHwnd, GWLP_WNDPROC);
        HookLog("[wnd] wndproc=%p", (void*)proc);
        if (!proc) return;
        MH_STATUS st = MH_CreateHook((LPVOID)proc, (LPVOID)&DetourWndProc, (LPVOID*)&g_origWndProc);
        HookLog("[wnd] hook st=%d", (int)st);
        if (st == MH_OK) HookLog("[wnd] enable st=%d", (int)MH_EnableHook((LPVOID)proc));
    }
    // ===== 方案3：无障碍接口探针（UIAutomationCore / Oleacc）=====
    typedef void* (*Fn_ReturnRaw)(void*, size_t, intptr_t, void*);
    typedef int   (*Fn_HostFromHwnd)(void*, void**);
    typedef intptr_t (*Fn_LresultFromObject)(const void*, size_t, void*);
    typedef int   (*Fn_AccObjFromWindow)(void*, unsigned long, const void*, void**);

    static Fn_ReturnRaw         g_orig_ReturnRaw = nullptr;
    static Fn_HostFromHwnd      g_orig_HostFromHwnd = nullptr;
    static Fn_LresultFromObject g_orig_LresultFromObject = nullptr;
    static Fn_AccObjFromWindow  g_orig_AccObjFromWindow = nullptr;
    static volatile LONG g_accHits = 0;

    static void LogAccObj(const char* tag, void* obj)
    {
        if (!obj) { HookLog("[acc] %s = null", tag); return; }
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt = 0;
        __try { vt = *(uint64_t*)obj; } __except (EXCEPTION_EXECUTE_HANDLER) { vt = 0; }
        HookLog("[acc] %s = %p vt=%llX rva=%llX", tag, obj, (unsigned long long)vt,
                (unsigned long long)((vt >= base && vt < base + 0x10000000ULL) ? vt - base : 0));
        if (vt && vt >= base && vt < base + 0x10000000ULL) {
            for (int k = 0; k < 8; ++k) {
                uint64_t fn = 0;
                __try { fn = *(uint64_t*)(vt + k * 8); } __except (EXCEPTION_EXECUTE_HANDLER) { break; }
                HookLog("[acc]   vt[%d] = %llX (rva=%llX)", k, (unsigned long long)fn,
                        (unsigned long long)((fn >= base && fn < base + 0x10000000ULL) ? fn - base : 0));
            }
        }
    }

    static void* Detour_ReturnRaw(void* hwnd, size_t wParam, intptr_t lParam, void* el)
    {
        LONG n = InterlockedIncrement(&g_accHits);
        if (n <= 6) {
            HookLog("[acc] UiaReturnRawElementProvider hwnd=%p wp=%llX lp=%lld", hwnd,
                    (unsigned long long)wParam, (long long)lParam);
            LogAccObj("provider", el);
        }
        // 主窗口的根 provider：缓存并持有引用，供“纯内部写输入框”遍历树使用
        if (el && InterlockedCompareExchange(&g_capturing, 1, 1) == 1) {
            uint64_t p = (uint64_t)el;
            if (ProvIsValidObj(p) && ProvAddRefSimple(p)) {
                InterlockedExchange64((volatile LONG64*)&g_rootProv, (LONG64)p);
                static volatile LONG s_logged = 0;
                if (InterlockedIncrement(&s_logged) <= 10)
                    HookLog("[internal] 捕获根 provider=%llX hwnd=%p", (unsigned long long)p, hwnd);
            }
        }
        return g_orig_ReturnRaw(hwnd, wParam, lParam, el);
    }

    static int Detour_HostFromHwnd(void* hwnd, void** out)
    {
        int rc = g_orig_HostFromHwnd(hwnd, out);
        LONG n = InterlockedIncrement(&g_accHits);
        if (n <= 6) {
            HookLog("[acc] UiaHostProviderFromHwnd hwnd=%p rc=%d", hwnd, rc);
            if (out) LogAccObj("provider", *out);
        }
        return rc;
    }

    static intptr_t Detour_LresultFromObject(const void* riid, size_t wParam, void* punk)
    {
        intptr_t rc = g_orig_LresultFromObject(riid, wParam, punk);
        LONG n = InterlockedIncrement(&g_accHits);
        if (n <= 6) {
            HookLog("[acc] LresultFromObject wParam=%llX", (unsigned long long)wParam);
            LogAccObj("punk", punk);
        }
        return rc;
    }

    static int Detour_AccObjFromWindow(void* hwnd, unsigned long id, const void* riid, void** out)
    {
        int rc = g_orig_AccObjFromWindow(hwnd, id, riid, out);
        LONG n = InterlockedIncrement(&g_accHits);
        if (n <= 6) {
            HookLog("[acc] AccessibleObjectFromWindow hwnd=%p id=%lu rc=%d", hwnd, id, rc);
            if (out) LogAccObj("accobj", *out);
        }
        return rc;
    }

    static void HookOne(const wchar_t* dllName, const char* fnName, void* detour, void** orig)
    {
        HMODULE h = GetModuleHandleW(dllName);
        if (!h) { HookLog("[acc] %ls 未加载", dllName); return; }
        void* fn = (void*)GetProcAddress(h, fnName);
        if (!fn) { HookLog("[acc] 导出不存在: %s", fnName); return; }
        MH_STATUS st = MH_CreateHook(fn, detour, orig);
        MH_STATUS st2 = (st == MH_OK) ? MH_EnableHook(fn) : st;
        HookLog("[acc] hook %s st=%d/%d", fnName, (int)st, (int)st2);
    }

    static void RegisterAccessibilityProbes()
    {
        HookOne(L"UIAutomationCore.dll", "UiaReturnRawElementProvider", (void*)&Detour_ReturnRaw, (void**)&g_orig_ReturnRaw);
        HookOne(L"UIAutomationCore.dll", "UiaHostProviderFromHwnd", (void*)&Detour_HostFromHwnd, (void**)&g_orig_HostFromHwnd);
        HookOne(L"Oleacc.dll", "LresultFromObject", (void*)&Detour_LresultFromObject, (void**)&g_orig_LresultFromObject);
        HookOne(L"Oleacc.dll", "AccessibleObjectFromWindow", (void*)&Detour_AccObjFromWindow, (void**)&g_orig_AccObjFromWindow);
    }

    // ===== 方案3：provider 方法钩子（定位 SetValue）=====
    #define PROV_METHOD_COUNT 7
    static const uintptr_t g_provRvAs[PROV_METHOD_COUNT] = {
        0x4C53AB0, 0x4C53BA0, 0x4C53BB0, 0x4C53C50,
        0x4C53C70, 0x4C541B0, 0x4C54890
    };

    // out 参数候选池：热路径只写内存（零 IO），由 HTTP /OutCands 取走分析
    struct OutCandRec {
        uint64_t val;      // out 参数写出的对象指针
        uint64_t vt;       // 该对象的虚表（绝对地址，须在 Weixin.dll 内）
        uint64_t self;     // 调用时的 this
        uint32_t key;      // 方法编号（0~13=p2, 16~22=prov, 64+=dyn）
        uint32_t argi;     // 参数下标（2/3/4）
    };
    static OutCandRec g_outCands[512];
    static volatile LONG g_outN = 0;

    // 安全可读性检查：先 VirtualQuery 预校验，避免投机读触发第一轮异常（VEH 记录会拖慢 provider）
    static bool IsReadablePtr(uint64_t p, uint64_t len)
    {
        if (p < 0x10000 || p > 0x00007FFFFFFFFFFFULL) return false;
        if (len == 0) return true;
        MEMORY_BASIC_INFORMATION mbi;
        if (VirtualQuery((void*)p, &mbi, sizeof(mbi)) != sizeof(mbi)) return false;
        if (mbi.State != MEM_COMMIT) return false;
        if (mbi.Protect & PAGE_GUARD) return false;
        DWORD prot = mbi.Protect & 0xFF;
        if (prot != PAGE_READONLY && prot != PAGE_READWRITE && prot != PAGE_WRITECOPY &&
            prot != PAGE_EXECUTE_READ && prot != PAGE_EXECUTE_READWRITE && prot != PAGE_EXECUTE_WRITECOPY)
            return false;
        return (p + len) <= ((uint64_t)mbi.BaseAddress + mbi.RegionSize);
    }

    static void LogOutCand(int key, int argi, uint64_t self, uint64_t paddr)
    {
        if (!IsReadablePtr(paddr, 8)) return;
        uint64_t val = 0;
        __try { memcpy(&val, (void*)paddr, 8); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return; }
        if (!IsReadablePtr(val, 8)) return;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt = 0;
        __try { memcpy(&vt, (void*)val, 8); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return; }
        if (!(vt >= base && vt < base + 0x10000000ULL)) return;
        int n = (int)g_outN;
        if (n > 512) return;
        for (int i = 0; i < n && i < 512; ++i) {
            if (g_outCands[i].val == val && g_outCands[i].vt == vt) return;   // 去重
        }
        int idx = InterlockedIncrement(&g_outN) - 1;
        if (idx >= 0 && idx < 512) {
            g_outCands[idx].val = val;
            g_outCands[idx].vt = vt;
            g_outCands[idx].self = self;
            g_outCands[idx].key = (uint32_t)key;
            g_outCands[idx].argi = (uint32_t)argi;
        }
    }

    // 每个候选导出 5 个 qword：val, vt, rva, self, (key<<8|argi)
    extern "C" __declspec(dllexport) int WxGetOutCands(unsigned long long* out, int maxOut)
    {
        if (!out || maxOut <= 0) return 0;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        int n = (int)g_outN;
        if (n > 512) n = 512;
        if (n > maxOut) n = maxOut;
        for (int i = 0; i < n; ++i) {
            out[i * 5 + 0] = g_outCands[i].val;
            out[i * 5 + 1] = g_outCands[i].vt;
            out[i * 5 + 2] = (g_outCands[i].vt >= base) ? (g_outCands[i].vt - base) : 0;
            out[i * 5 + 3] = g_outCands[i].self;
            out[i * 5 + 4] = ((uint64_t)g_outCands[i].key << 8) | g_outCands[i].argi;
        }
        return n;
    }


    static void LogWstrCand(const char* tag, int argi, uint64_t p)
    {
        if (!IsReadablePtr(p, sizeof(wchar_t) * 48)) return;
        wchar_t wbuf[128] = {0};
        __try { memcpy(wbuf, (void*)p, sizeof(wchar_t) * 64); }
        __except (EXCEPTION_EXECUTE_HANDLER) { wbuf[0] = 0; }
        bool printable = wbuf[0] != 0;
        for (int i = 0; i < 64 && wbuf[i]; ++i) {
            if (wbuf[i] < 32 || wbuf[i] > 0xFFFE) { printable = false; break; }
        }
        if (printable) {
            char utf8[256] = {0};
            WideCharToMultiByte(CP_UTF8, 0, wbuf, -1, utf8, sizeof(utf8) - 1, nullptr, nullptr);
            HookLog("%s a%d as wstr = %s", tag, argi, utf8);
        }
    }

    static void LogProvCall(int idx, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
    {
        HookLog("[prov] method[%d] this=%llX a2=%llX a3=%llX a4=%llX", idx,
                (unsigned long long)a1, (unsigned long long)a2,
                (unsigned long long)a3, (unsigned long long)a4);
        LogWstrCand("[prov]", 2, a2);
        LogWstrCand("[prov]", 3, a3);
        LogWstrCand("[prov]", 4, a4);
    }

    typedef uint64_t(*FnProv)(uint64_t, uint64_t, uint64_t, uint64_t);

    #define DEF_PROV(VAR, ORIG, IDX)                                                    \
    static FnProv ORIG = nullptr;                                                       \
    static volatile LONG g_provHits_##VAR = 0;                                          \
    static uint64_t Detour_##VAR(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)    \
    {                                                                                   \
        LONG n = InterlockedIncrement(&g_provHits_##VAR);                               \
        if (n <= 5) LogProvCall(IDX, a1, a2, a3, a4);                                   \
        uint64_t rr = ORIG(a1, a2, a3, a4);                                             \
        LogOutCand(16 + IDX, 2, a1, a2);                                                \
        LogOutCand(16 + IDX, 3, a1, a3);                                                \
        LogOutCand(16 + IDX, 4, a1, a4);                                                \
        if (n <= 5 && rr > 0x10000) HookLog("[prov] ret=%llX", (unsigned long long)rr);  \
        return rr;                                                                      \
    }

    DEF_PROV(p0, g_op0, 0)
    DEF_PROV(p1, g_op1, 1)
    DEF_PROV(p2, g_op2, 2)
    DEF_PROV(p3, g_op3, 3)
    DEF_PROV(p4, g_op4, 4)
    DEF_PROV(p5, g_op5, 5)
    DEF_PROV(p6, g_op6, 6)

    static void RegisterProviderMethods()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* detours[PROV_METHOD_COUNT] = {
            (void*)&Detour_p0, (void*)&Detour_p1, (void*)&Detour_p2, (void*)&Detour_p3,
            (void*)&Detour_p4, (void*)&Detour_p5, (void*)&Detour_p6
        };
        void** origs[PROV_METHOD_COUNT] = {
            (void**)&g_op0, (void**)&g_op1, (void**)&g_op2, (void**)&g_op3,
            (void**)&g_op4, (void**)&g_op5, (void**)&g_op6
        };
        for (int i = 0; i < PROV_METHOD_COUNT; ++i) {
            void* fn = (void*)(base + g_provRvAs[i]);
            MH_STATUS st = MH_CreateHook(fn, detours[i], origs[i]);
            MH_STATUS st2 = (st == MH_OK) ? MH_EnableHook(fn) : st;
            HookLog("[prov] hook method[%d] rva=%llX st=%d/%d", i,
                    (unsigned long long)g_provRvAs[i], (int)st, (int)st2);
        }
    }

    // ===== 方案3：另外两个接口虚表方法钩子（找 IValueProvider::SetValue）=====
    static const uintptr_t g_p2RvAs[] = {
        0x4C53B90, 0xBD7A20, 0x4C53C00, 0x4C54A10, 0x4C54D50, 0x4C54F20,
        0x4C550B0, 0x4C55200, 0x4C55370, 0x4C55B00, 0x261CF10,
        0x4C55B10, 0x4C55500, 0x4C55980
    };
    static const int g_p2Count = (int)(sizeof(g_p2RvAs) / sizeof(g_p2RvAs[0]));

    static void LogP2Call(int idx, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
    {
        HookLog("[p2] m[%d] this=%llX a2=%llX a3=%llX a4=%llX", idx,
                (unsigned long long)a1, (unsigned long long)a2,
                (unsigned long long)a3, (unsigned long long)a4);
        for (int k = 2; k <= 4; ++k) {
            uint64_t p = (k == 2) ? a2 : (k == 3) ? a3 : a4;
            if (!IsReadablePtr(p, sizeof(wchar_t) * 48)) continue;
            wchar_t wb[96] = {0};
            __try { memcpy(wb, (void*)p, sizeof(wchar_t) * 48); }
            __except (EXCEPTION_EXECUTE_HANDLER) { wb[0] = 0; }
            bool ok = wb[0] != 0;
            for (int i = 0; i < 48 && wb[i]; ++i) {
                if (wb[i] < 32 || wb[i] > 0x7E) { ok = false; break; }
            }
            if (ok) {
                char u8[256] = {0};
                WideCharToMultiByte(CP_UTF8, 0, wb, -1, u8, sizeof(u8) - 1, nullptr, nullptr);
                HookLog("[p2]   a%d as wstr = %s", k, u8);
            }
        }
    }

    typedef uint64_t(*FnP2)(uint64_t, uint64_t, uint64_t, uint64_t);

    static void LogP2Ret(int idx, uint64_t self, uint64_t rr)
    {
        if (!IsReadablePtr(rr, 8)) return;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt = 0;
        __try { vt = *(uint64_t*)rr; } __except (EXCEPTION_EXECUTE_HANDLER) { vt = 0; }
        if (!(vt >= base && vt < base + 0x10000000ULL)) return;   // 只记虚表落在 Weixin.dll 内的返回对象（fragment 候选）
        static volatile LONG s_n = 0;
        if (InterlockedIncrement(&s_n) > 400) return;
        HookLog("[p2] ret=%llX this=%llX m[%d] vt=%llX rva=%llX",
                (unsigned long long)rr, (unsigned long long)self, idx,
                (unsigned long long)vt, (unsigned long long)(vt - base));
    }

    #define DEF_P2(NAME, IDX)                                                          \
    static FnP2 g_o2_##NAME = nullptr;                                                 \
    static volatile LONG g_h2_##NAME = 0;                                              \
    static uint64_t s_last_##NAME = 0;                                                 \
    static uint64_t D2_##NAME(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)      \
    {                                                                                  \
        LONG n = InterlockedIncrement(&g_h2_##NAME);                                   \
        if (n <= 3) LogP2Call(IDX, a1, a2, a3, a4);                                    \
        uint64_t rr = g_o2_##NAME(a1, a2, a3, a4);                                     \
        LogOutCand(IDX, 2, a1, a2);                                                    \
        LogOutCand(IDX, 3, a1, a3);                                                    \
        LogOutCand(IDX, 4, a1, a4);                                                    \
        if (rr > 0x10000 && rr != s_last_##NAME) {                                     \
            s_last_##NAME = rr;                                                        \
            LogP2Ret(IDX, a1, rr);                                                     \
        }                                                                              \
        return rr;                                                                     \
    }

    DEF_P2(q0, 0)  DEF_P2(q1, 1)  DEF_P2(q2, 2)  DEF_P2(q3, 3)
    DEF_P2(q4, 4)  DEF_P2(q5, 5)  DEF_P2(q6, 6)  DEF_P2(q7, 7)
    DEF_P2(q8, 8)  DEF_P2(q9, 9)  DEF_P2(q10, 10) DEF_P2(q11, 11)
    DEF_P2(q12, 12) DEF_P2(q13, 13)

    static void RegisterProviderMethods2()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        void* detours[] = {
            (void*)&D2_q0,(void*)&D2_q1,(void*)&D2_q2,(void*)&D2_q3,(void*)&D2_q4,
            (void*)&D2_q5,(void*)&D2_q6,(void*)&D2_q7,(void*)&D2_q8,(void*)&D2_q9,
            (void*)&D2_q10,(void*)&D2_q11,(void*)&D2_q12,(void*)&D2_q13
        };
        void** origs[] = {
            (void**)&g_o2_q0,(void**)&g_o2_q1,(void**)&g_o2_q2,(void**)&g_o2_q3,(void**)&g_o2_q4,
            (void**)&g_o2_q5,(void**)&g_o2_q6,(void**)&g_o2_q7,(void**)&g_o2_q8,(void**)&g_o2_q9,
            (void**)&g_o2_q10,(void**)&g_o2_q11,(void**)&g_o2_q12,(void**)&g_o2_q13
        };
        for (int i = 0; i < g_p2Count; ++i) {
            void* fn = (void*)(base + g_p2RvAs[i]);
            MH_STATUS st = MH_CreateHook(fn, detours[i], origs[i]);
            MH_STATUS st2 = (st == MH_OK) ? MH_EnableHook(fn) : st;
            HookLog("[p2] hook m[%d] rva=%llX st=%d/%d", i, (unsigned long long)g_p2RvAs[i], (int)st, (int)st2);
        }
    }

    static DWORD WINAPI RegThread(LPVOID)
    {
        Sleep(9000);
        HookLog("[hookreg] begin");
        Register();        Sleep(500);
        // Reg_fn1C11050();  // 改用 RegisterCallerProbe()，避免 MH_ERROR_ALREADY_CREATED
        RegisterCallerProbe();
        RegisterEnterProbe();
        RegisterWndProcHook();
        RegisterAccessibilityProbes();
        RegisterProviderMethods();
        RegisterProviderMethods2();
        HookLog("[hookreg] done");
        // CreateThread(nullptr, 0, ScanThread, nullptr, 0, nullptr);  // 已停用：日志刷屏，改用 /MemSearch 系列接口
        return 0;
    }

    void RegisterAllAsync()
    {
        CreateThread(nullptr, 0, RegThread, nullptr, 0, nullptr);
    }
    // ===== 动态挂接：对任意 vtable 的 N 个方法批量挂钩（无需重启，用于 fragment 接口探测）=====
    #define DYN_SLOT_COUNT 24
    typedef uint64_t(*FnDyn)(uint64_t, uint64_t, uint64_t, uint64_t);
    static FnDyn g_dynOrig[DYN_SLOT_COUNT] = {nullptr};
    static volatile LONG g_dynHits[DYN_SLOT_COUNT] = {0};
    static uint64_t g_dynTarget[DYN_SLOT_COUNT] = {0};
    static int g_dynUsed = 0;
    static CRITICAL_SECTION g_dynCs;
    static volatile LONG g_dynCsInit = 0;

    static void EnsureDynCs()
    {
        if (InterlockedCompareExchange(&g_dynCsInit, 1, 0) == 0) {
            InitializeCriticalSection(&g_dynCs);
        }
    }

    static void LogDynCall(int slot, uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4)
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt = 0;
        if (IsReadablePtr(a1, 8)) { __try { vt = *(uint64_t*)a1; } __except (EXCEPTION_EXECUTE_HANDLER) { vt = 0; } }
        HookLog("[dyn] s[%d] this=%llX vt=%llX rva=%llX a2=%llX a3=%llX a4=%llX",
                slot, (unsigned long long)a1, (unsigned long long)vt,
                (unsigned long long)((vt >= base && vt < base + 0x10000000ULL) ? vt - base : 0),
                (unsigned long long)a2, (unsigned long long)a3, (unsigned long long)a4);
        LogWstrCand("[dyn]", 2, a2);
        LogWstrCand("[dyn]", 3, a3);
        LogWstrCand("[dyn]", 4, a4);
    }

    #define DEF_DYN(SLOT)                                                              \
    static uint64_t DynDetour##SLOT(uint64_t a1, uint64_t a2, uint64_t a3, uint64_t a4) \
    {                                                                                  \
        LONG n = InterlockedIncrement(&g_dynHits[SLOT]);                               \
        if (n <= 6) LogDynCall(SLOT, a1, a2, a3, a4);                                  \
        uint64_t rr = g_dynOrig[SLOT](a1, a2, a3, a4);                                 \
        LogOutCand(64 + SLOT, 2, a1, a2);                                              \
        LogOutCand(64 + SLOT, 3, a1, a3);                                              \
        LogOutCand(64 + SLOT, 4, a1, a4);                                              \
        if (n <= 6 && rr > 0x10000) HookLog("[dyn] s[%d] ret=%llX", SLOT, (unsigned long long)rr); \
        return rr;                                                                     \
    }

    DEF_DYN(0)  DEF_DYN(1)  DEF_DYN(2)  DEF_DYN(3)  DEF_DYN(4)  DEF_DYN(5)
    DEF_DYN(6)  DEF_DYN(7)  DEF_DYN(8)  DEF_DYN(9)  DEF_DYN(10) DEF_DYN(11)
    DEF_DYN(12) DEF_DYN(13) DEF_DYN(14) DEF_DYN(15) DEF_DYN(16) DEF_DYN(17)
    DEF_DYN(18) DEF_DYN(19) DEF_DYN(20) DEF_DYN(21) DEF_DYN(22) DEF_DYN(23)

    static void* g_dynDetours[DYN_SLOT_COUNT] = {
        (void*)&DynDetour0,  (void*)&DynDetour1,  (void*)&DynDetour2,  (void*)&DynDetour3,
        (void*)&DynDetour4,  (void*)&DynDetour5,  (void*)&DynDetour6,  (void*)&DynDetour7,
        (void*)&DynDetour8,  (void*)&DynDetour9,  (void*)&DynDetour10, (void*)&DynDetour11,
        (void*)&DynDetour12, (void*)&DynDetour13, (void*)&DynDetour14, (void*)&DynDetour15,
        (void*)&DynDetour16, (void*)&DynDetour17, (void*)&DynDetour18, (void*)&DynDetour19,
        (void*)&DynDetour20, (void*)&DynDetour21, (void*)&DynDetour22, (void*)&DynDetour23
    };

    static int WxHookVtableImpl(unsigned long long vtAddr, int n)
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        if (!base) return -1;
        if (vtAddr < base || vtAddr >= base + 0x10000000ULL) return -2;
        if (n <= 0) n = 8;
        if (n > DYN_SLOT_COUNT) n = DYN_SLOT_COUNT;
        EnsureDynCs();
        EnterCriticalSection(&g_dynCs);
        int hooked = 0;
        for (int i = 0; i < n && g_dynUsed < DYN_SLOT_COUNT; ++i) {
            uint64_t fn = 0;
            __try { fn = *(uint64_t*)(vtAddr + (uint64_t)i * 8); }
            __except (EXCEPTION_EXECUTE_HANDLER) { fn = 0; }
            if (fn < base || fn >= base + 0x10000000ULL) {
                HookLog("[dyn] m[%d] fn=%llX 非模块内，跳过", i, (unsigned long long)fn);
                continue;
            }
            int slot = g_dynUsed;
            MH_STATUS st = MH_CreateHook((void*)fn, g_dynDetours[slot], (void**)&g_dynOrig[slot]);
            if (st != MH_OK) {
                HookLog("[dyn] create fail s[%d] m[%d] fn_rva=%llX st=%d", slot, i,
                        (unsigned long long)(fn - base), (int)st);
                continue;
            }
            MH_STATUS st2 = MH_EnableHook((void*)fn);
            if (st2 != MH_OK) {
                HookLog("[dyn] enable fail s[%d] m[%d] fn_rva=%llX st=%d", slot, i,
                        (unsigned long long)(fn - base), (int)st2);
                continue;
            }
            g_dynTarget[slot] = fn;
            g_dynUsed++;
            hooked++;
            HookLog("[dyn] hook s[%d] m[%d] fn_rva=%llX st=0/0", slot, i,
                    (unsigned long long)(fn - base));
        }
        LeaveCriticalSection(&g_dynCs);
        return hooked;
    }

    extern "C" __declspec(dllexport) int WxHookVtable(unsigned long long vtAddr, int n)
    {
        return WxHookVtableImpl(vtAddr, n);
    }

    extern "C" __declspec(dllexport) int WxDynReset()
    {
        EnsureDynCs();
        EnterCriticalSection(&g_dynCs);
        int n = 0;
        for (int s = 0; s < g_dynUsed; ++s) {
            if (!g_dynTarget[s]) continue;
            MH_DisableHook((void*)g_dynTarget[s]);
            MH_RemoveHook((void*)g_dynTarget[s]);
            g_dynTarget[s] = 0;
            g_dynOrig[s] = nullptr;
            n++;
        }
        g_dynUsed = 0;
        LeaveCriticalSection(&g_dynCs);
        return n;
    }

    // ===================== 纯内部写输入框（目标1：去掉 UIA 校准） =====================
    // 微信自身的 UIA provider 是进程内实现（Qt QWindowsUiaMainProvider）。
    // 在进程内触发 WM_GETOBJECT 取根 provider，用 IRawElementProviderFragment::Navigate
    // 遍历树，按 ClassName 找到 mmui::ChatInputField，再 GetPatternProvider(ValuePattern)
    // 直接调用 IValueProvider::SetValue 写入文本。全程无需外部 UIA 客户端。
    // 已确认的 ABI（Qt 5.15 / 本版本）：
    //   Simple      = provider 基址+0x18，vtable rva 0x9933CD8，[1]=AddRef [2]=Release
    //                 [4]=GetPatternProvider [5]=GetPropertyValue
    //   Fragment    = provider 基址+0x20，vtable rva 0x9933D18，[3]=Navigate
    //   FragmentRoot= provider 基址+0x28，vtable rva 0x9933D68
    //   IValueProvider：rva 0x9C424F8，[3]=SetValue [4]=get_Value [5]=get_IsReadOnly
    typedef uint64_t(*FnProv4)(uint64_t, uint64_t, uint64_t, uint64_t);

    static uint64_t ProvCallVt(uint64_t obj, int idx, uint64_t a2, uint64_t a3)
    {
        if (!obj || !IsReadablePtr(obj, 8)) return 0;
        uint64_t vt = 0;
        memcpy(&vt, (void*)obj, 8);
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        if (vt < base || vt >= base + 0x10000000ULL) return 0;
        if (!IsReadablePtr(vt + (uint64_t)idx * 8, 8)) return 0;
        uint64_t fn = 0;
        memcpy(&fn, (void*)(vt + (uint64_t)idx * 8), 8);
        if (fn < base || fn >= base + 0x10000000ULL) return 0;
        uint64_t rr = 0;
        __try { rr = ((FnProv4)fn)(obj, a2, a3, 0); }
        __except (EXCEPTION_EXECUTE_HANDLER) { rr = 0; }
        return rr;
    }

    static bool ProvIsValidObj(uint64_t simple)
    {
        if (!simple || !IsReadablePtr(simple, 0x30)) return false;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt0 = 0, vt1 = 0, vt2 = 0;
        memcpy(&vt0, (void*)simple, 8);
        memcpy(&vt1, (void*)(simple + 8), 8);
        memcpy(&vt2, (void*)(simple + 0x10), 8);
        return vt0 == base + 0x9933CD8ULL && vt1 == base + 0x9933D18ULL && vt2 == base + 0x9933D68ULL;
    }

    static uint64_t ProvAddRefSimple(uint64_t simple)
    {
        if (!ProvIsValidObj(simple)) return 0;
        return ProvCallVt(simple, 1, 0, 0);
    }

    // UIA 属性读取（Simple[5] = GetPropertyValue，out 为 VARIANT）
    static bool ProvGetStrProp(uint64_t simple, int propId, wchar_t* out, int cap)
    {
        VARIANT var;
        VariantInit(&var);
        bool ok = false;
        uint64_t hr = ProvCallVt(simple, 5, (uint64_t)propId, (uint64_t)&var);
        if (((uint32_t)hr == 0) && var.vt == VT_BSTR && var.bstrVal != nullptr) {
            if (cap > 0) { wcsncpy_s(out, (size_t)cap, var.bstrVal, _TRUNCATE); ok = true; }
        }
        VariantClear(&var);
        return ok;
    }

    static bool ProvGetI4Prop(uint64_t simple, int propId, int* out)
    {
        VARIANT var;
        VariantInit(&var);
        bool ok = false;
        uint64_t hr = ProvCallVt(simple, 5, (uint64_t)propId, (uint64_t)&var);
        if (((uint32_t)hr == 0) && var.vt == VT_I4) { *out = var.lVal; ok = true; }
        VariantClear(&var);
        return ok;
    }

    // 激活 Qt 无障碍门控（Weixin.dll+0xb39c9d8 = 1），否则 provider 树不物化
    // 注意：微信会把它复位为 0，所以每次取根前都重新写一次
    static void EnsureAccGate()
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        BYTE* p = (BYTE*)(base + 0xB39C9D8ULL);
        if (!IsReadablePtr((uint64_t)p, 1)) return;
        DWORD old = 0;
        if (!VirtualProtect(p, 1, PAGE_READWRITE, &old)) return;
        BYTE before = *p;
        if (before != 1) *p = 1;
        DWORD tmp = 0;
        VirtualProtect(p, 1, old, &tmp);
        if (before != 1) HookLog("[internal] 无障碍门控 rva=b39c9d8 %d -> 1", (int)before);
        InterlockedExchange(&g_accGateOn, 1);
    }

    // 通过 Qt 的 accessibleInterface()（rva 0x66266A0，参数为 provider 基址）校验元素是否仍存活
    static bool ProvIsAlive(uint64_t simple)
    {
        if (!ProvIsValidObj(simple)) return false;
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t providerBase = simple - 0x18;
        FnProv4 f = (FnProv4)(base + 0x66266A0ULL);
        uint64_t acc = 0;
        __try { acc = f(providerBase, 0, 0, 0); }
        __except (EXCEPTION_EXECUTE_HANDLER) { acc = 0; }
        return acc != 0;
    }

    static HWND FindWxWindowAny(int index)
    {
        DWORD mypid = GetCurrentProcessId();
        int seen = 0;
        HWND w = nullptr;
        while ((w = FindWindowExW(nullptr, w, nullptr, L"微信")) != nullptr) {
            DWORD pid = 0;
            GetWindowThreadProcessId(w, &pid);
            if (pid != mypid || !IsWindowVisible(w)) continue;
            if (seen++ == index) return w;
        }
        w = nullptr;
        while ((w = FindWindowExW(nullptr, w, L"Qt51514QWindowIcon", nullptr)) != nullptr) {
            DWORD pid = 0;
            GetWindowThreadProcessId(w, &pid);
            if (pid != mypid || !IsWindowVisible(w)) continue;
            if (seen++ == index) return w;
        }
        return nullptr;
    }

    // 向指定窗口触发 WM_GETOBJECT（lParam=UiaRootObjectId），由 Detour_ReturnRaw 捕获根 provider
    static int CaptureRootFrom(HWND h)
    {
        if (!h || !IsWindow(h)) return -1;
        EnsureAccGate();
        for (int attempt = 0; attempt < 2; ++attempt) {
            g_rootProv = 0;
            DWORD_PTR res = 0;
            InterlockedExchange(&g_capturing, 1);
            SendMessageTimeoutW(h, WM_GETOBJECT, (WPARAM)(attempt == 0 ? 0 : (ULONG_PTR)0xFFFFFFFCULL),
                                (LPARAM)(LONG_PTR)(-25), SMTO_ABORTIFHUNG | SMTO_BLOCK, 3000, &res);
            InterlockedExchange(&g_capturing, 0);
            if (g_rootProv && ProvIsValidObj(g_rootProv) && ProvIsAlive(g_rootProv)) {
                HookLog("[internal] 取根成功 hwnd=%p provider=%llX", h, (unsigned long long)g_rootProv);
                return 0;
            }
        }
        return -2;
    }

    // 把函数投递到微信 UI 线程执行（触控 Qt/GUI 状态的调用必须在这里，见 failure_ledger #10）
    static int RunOnUiThread(void (*fn)(void*), void* arg, int timeoutMs)
    {
        HWND h = g_wxHwnd;
        if (!h || !IsWindow(h)) {
            h = FindWxWindowAny(0);
            if (h && !g_origWndProc) {
                WNDPROC proc = (WNDPROC)GetWindowLongPtrW(h, GWLP_WNDPROC);
                if (proc) {
                    MH_STATUS st = MH_CreateHook((LPVOID)proc, (LPVOID)&DetourWndProc, (LPVOID*)&g_origWndProc);
                    MH_STATUS st2 = (st == MH_OK) ? MH_EnableHook((LPVOID)proc) : st;
                    HookLog("[internal] 补挂 WndProc hwnd=%p st=%d/%d", h, (int)st, (int)st2);
                    if (st2 == MH_OK) g_wxHwnd = h;
                }
            } else if (h) {
                g_wxHwnd = h;
            }
        }
        if (!h || !g_origWndProc || !IsWindow(h)) return -1;
        g_uiTaskFn = fn;
        g_uiTaskArg = arg;
        InterlockedExchange(&g_uiTaskDone, 0);
        InterlockedExchange(&g_uiTaskPending, 1);
        DWORD_PTR res = 0;
        LRESULT rr = SendMessageTimeoutW(h, WM_NULL, 0, 0, SMTO_ABORTIFHUNG | SMTO_BLOCK,
                                         (DWORD)timeoutMs, &res);
        if (rr == 0) { InterlockedExchange(&g_uiTaskPending, 0); HookLog("[internal] UI 投递超时"); return -2; }
        if (InterlockedCompareExchange(&g_uiTaskDone, 1, 1) != 1) { HookLog("[internal] UI 任务未执行"); return -3; }
        return 0;
    }

    #define PROV_WALK_MAX 800

    struct ProvWalkInfo {
        int visited;
        int found;
        uint64_t obj;         // 匹配到的输入框元素（Simple 接口），持有 1 个引用
        wchar_t cls[128];
        wchar_t name[128];
        int ctrlType;
    };

    static int WalkFromRoot(ProvWalkInfo* info)
    {
        static uint64_t queue[PROV_WALK_MAX];
        int n = 0, qi = 0;
        if (!ProvAddRefSimple(g_rootProv)) return -3;
        if (!ProvIsAlive(g_rootProv)) return -3;
        queue[n++] = g_rootProv;
        while (qi < n && info->visited < PROV_WALK_MAX) {
            uint64_t simple = queue[qi++];
            info->visited++;
            if (!ProvIsValidObj(simple)) continue;
            wchar_t cls[128] = {0};
            bool gotCls = ProvGetStrProp(simple, 0x753C /*ClassName*/, cls, 128);
            if (info->visited <= 40) {
                char u8[256] = {0};
                WideCharToMultiByte(CP_UTF8, 0, cls, -1, u8, sizeof(u8) - 1, nullptr, nullptr);
                HookLog("[internal] node#%d obj=%llX cls=%s", info->visited, (unsigned long long)simple, u8);
            }
            if (gotCls && _wcsicmp(cls, L"mmui::ChatInputField") == 0) {
                info->visited = info->visited;   // 保持
                info->found = 1;
                info->obj = simple;
                wcscpy_s(info->cls, cls);
                ProvGetStrProp(simple, 0x7535 /*Name*/, info->name, 128);
                ProvGetI4Prop(simple, 0x7533 /*ControlType*/, &info->ctrlType);
                for (int k = qi; k < n; ++k) ProvCallVt(queue[k], 2, 0, 0);
                return 0;
            }
            uint64_t frag = simple + 8;
            uint64_t child = 0;
            ProvCallVt(frag, 3, 3 /*FirstChild*/, (uint64_t)&child);
            while (child) {
                uint64_t cs = child - 8;
                uint64_t sib = 0;
                if (!ProvIsValidObj(cs)) { ProvCallVt(child, 2, 0, 0); break; }
                if (n < PROV_WALK_MAX) queue[n++] = cs;
                else ProvCallVt(child, 2, 0, 0);
                ProvCallVt(child, 3, 1 /*NextSibling*/, (uint64_t)&sib);
                child = sib;
            }
            ProvCallVt(simple, 2, 0, 0);
        }
        for (int k = qi; k < n; ++k) ProvCallVt(queue[k], 2, 0, 0);
        return -4;
    }

    static int ProvFindInput(ProvWalkInfo* info)
    {
        if (!info) return -1;
        memset(info, 0, sizeof(*info));
        int visitedAll = 0;
        for (int wi = 0; wi < 6; ++wi) {
            HWND h = FindWxWindowAny(wi);
            if (!h) break;
            info->visited = 0;
            info->found = 0;
            info->obj = 0;
            info->cls[0] = 0;
            if (CaptureRootFrom(h) != 0) { visitedAll += info->visited; continue; }
            int rc = WalkFromRoot(info);
            visitedAll += info->visited;
            if (rc == 0) { info->visited = visitedAll; return 0; }
        }
        info->visited = visitedAll;
        return (visitedAll > 0) ? -4 : -2;
    }

    static int ProvSetInputText(const wchar_t* text, ProvWalkInfo* info,
                                wchar_t* before, int beforeCap, wchar_t* after, int afterCap)
    {
        int rc = ProvFindInput(info);
        if (rc != 0) return rc;
        uint64_t simple = info->obj;
        if (before && beforeCap > 0) before[0] = 0;
        if (after && afterCap > 0) after[0] = 0;
        uint64_t punk = 0;
        uint64_t hr = ProvCallVt(simple, 4 /*GetPatternProvider*/, 10002 /*UIA_ValuePatternId*/, (uint64_t)&punk);
        if (((uint32_t)hr != 0) || !punk) { ProvCallVt(simple, 2, 0, 0); return -5; }
        BSTR b = nullptr;
        if (((uint32_t)ProvCallVt(punk, 4 /*get_Value*/, (uint64_t)&b, 0) == 0) && b) {
            if (before && beforeCap > 0) wcsncpy_s(before, (size_t)beforeCap, b, _TRUNCATE);
        }
        if (b) { SysFreeString(b); b = nullptr; }
        hr = ProvCallVt(punk, 3 /*SetValue*/, (uint64_t)text, 0);
        b = nullptr;
        if (((uint32_t)ProvCallVt(punk, 4 /*get_Value*/, (uint64_t)&b, 0) == 0) && b) {
            if (after && afterCap > 0) wcsncpy_s(after, (size_t)afterCap, b, _TRUNCATE);
        }
        if (b) SysFreeString(b);
        ProvCallVt(punk, 2, 0, 0);    // Release IValueProvider
        ProvCallVt(simple, 2, 0, 0);  // Release element provider
        return ((uint32_t)hr == 0) ? 0 : -6;
    }

    struct SetInputTask {
        wchar_t text[4096];
        int rc;
        ProvWalkInfo info;
        wchar_t before[1024];
        wchar_t after[1024];
    };

    static SetInputTask* g_setInputTask = nullptr;

    static void SetInputTaskFn(void* p)
    {
        SetInputTask* t = (SetInputTask*)p;
        t->rc = ProvSetInputText(t->text, &t->info, t->before, 1024, t->after, 1024);
    }

    extern "C" __declspec(dllexport) int WxSetInputInternal(const char* utf8Text,
            char* outBefore, int capBefore, char* outAfter, int capAfter,
            unsigned long long* outObj, int* outVisited)
    {
        if (!utf8Text) return -1;
        static SetInputTask s_task;
        SetInputTask* task = &s_task;
        memset(task, 0, sizeof(*task));
        if (MultiByteToWideChar(CP_UTF8, 0, utf8Text, -1, task->text, 4096) <= 0) return -1;

        // 1) 在调用线程触发 WM_GETOBJECT 抓根 provider（避免 UI 线程重入 WndProc）
        int cr = -1;
        for (int wi = 0; wi < 6; ++wi) {
            HWND h = FindWxWindowAny(wi);
            if (!h) break;
            if (CaptureRootFrom(h) == 0) { cr = 0; break; }
        }
        if (cr != 0) { HookLog("[internal] SetInput 抓根失败"); return -2; }
        g_setInputTask = task;

        // 2) 遍历 + SetValue 投递到 UI 线程（Qt/GUI 状态必须线程亲和）
        int run = RunOnUiThread(SetInputTaskFn, task, 20000);
        if (run != 0) {
            HookLog("[internal] UI 线程投递失败(%d)，退化到调用线程（不稳定）", run);
            task->rc = ProvSetInputText(task->text, &task->info, task->before, 1024, task->after, 1024);
        }

        if (outBefore && capBefore > 0) {
            outBefore[0] = 0;
            WideCharToMultiByte(CP_UTF8, 0, task->before, -1, outBefore, capBefore, nullptr, nullptr);
        }
        if (outAfter && capAfter > 0) {
            outAfter[0] = 0;
            WideCharToMultiByte(CP_UTF8, 0, task->after, -1, outAfter, capAfter, nullptr, nullptr);
        }
        if (outObj) *outObj = task->info.obj;
        if (outVisited) *outVisited = task->info.visited;
        {
            char u8cls[256] = {0};
            WideCharToMultiByte(CP_UTF8, 0, task->info.cls, -1, u8cls, sizeof(u8cls) - 1, nullptr, nullptr);
            HookLog("[internal] SetInput rc=%d run=%d visited=%d obj=%llX cls=%s", task->rc, run,
                    task->info.visited, (unsigned long long)task->info.obj, u8cls);
        }
        return task->rc;
    }

}
// ===================== 探针结束 =====================

// ===================== 进程内内存搜索（用于定位 Qt 编辑器对象） =====================
extern "C" __declspec(dllexport) int WxMemSearch(const char* needle, unsigned long long* outAddrs,
                                                int maxOut, int* outTypes, unsigned long long* outRegionBases)
{
    if (!needle || !outAddrs) return -1;
    size_t nlen = strlen(needle);
    if (nlen < 3 || nlen > 128) return -2;

    unsigned char ascii[129] = { 0 };
    unsigned char wide[260] = { 0 };
    memcpy(ascii, needle, nlen);
    for (size_t i = 0; i < nlen; ++i) wide[i * 2] = (unsigned char)needle[i];
    size_t wlen = nlen * 2;

    int found = 0;
    unsigned char* addr = nullptr;
    MEMORY_BASIC_INFORMATION mbi;
    while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi) && found < maxOut) {
        bool readable = (mbi.State == MEM_COMMIT) &&
            (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                            PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) &&
            !(mbi.Protect & PAGE_GUARD);
        if (readable) {
            unsigned char* base = (unsigned char*)mbi.BaseAddress;
            size_t size = mbi.RegionSize;
            const size_t CHUNK = 0x40000;
            for (size_t off = 0; off < size && found < maxOut; off += CHUNK) {
                size_t chunk = (size - off < CHUNK) ? (size - off) : CHUNK;
                unsigned char* p = base + off;
                __try {
                    for (size_t i = 0; i + nlen <= chunk && found < maxOut; ++i) {
                        if (p[i] == ascii[0] && memcmp(p + i, ascii, nlen) == 0) {
                            outAddrs[found] = (unsigned long long)(p + i);
                            outTypes[found] = 1;
                            if (outRegionBases) outRegionBases[found] = (unsigned long long)base;
                            found++;
                        }
                    }
                    for (size_t i = 0; i + wlen <= chunk && found < maxOut; ++i) {
                        if (p[i] == wide[0] && memcmp(p + i, wide, wlen) == 0) {
                            outAddrs[found] = (unsigned long long)(p + i);
                            outTypes[found] = 2;
                            if (outRegionBases) outRegionBases[found] = (unsigned long long)base;
                            found++;
                        }
                    }
                }
                __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            }
        }
        unsigned char* next = (unsigned char*)mbi.BaseAddress + mbi.RegionSize;
        if (next <= addr) break;
        addr = next;
    }
    return found;
}
// 指针反查：搜索内存中等于给定 8 字节值的 qword（用于从 QString 数据反查持有者）
// 获取当前线程栈范围（用于在内存搜索中排除自身栈，避免"搜到自己的参数"）
static void GetCurStackLimits(unsigned long long* lo, unsigned long long* hi)
{
    *lo = 0; *hi = 0;
    HMODULE k = GetModuleHandleW(L"kernel32.dll");
    if (!k) return;
    typedef void(WINAPI* PFN)(PULONG_PTR, PULONG_PTR);
    PFN f = (PFN)GetProcAddress(k, "GetCurrentThreadStackLimits");
    if (!f) return;
    ULONG_PTR a = 0, b = 0;
    f(&a, &b);
    *lo = (unsigned long long)a; *hi = (unsigned long long)b;
}
extern "C" __declspec(dllexport) int WxMemSearchPtr(unsigned long long value, unsigned long long* outAddrs, int maxOut)
{
    int found = 0;
    unsigned char* addr = nullptr;
    MEMORY_BASIC_INFORMATION mbi;
    while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi) && found < maxOut) {
        bool readable = (mbi.State == MEM_COMMIT) &&
            (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                            PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) &&
            !(mbi.Protect & PAGE_GUARD);
        unsigned long long slo = 0, shi = 0;
        GetCurStackLimits(&slo, &shi);
        bool inOwnStack = false;
        {
            unsigned long long rlo = (unsigned long long)mbi.BaseAddress;
            unsigned long long rhi = rlo + mbi.RegionSize;
            if (shi > slo && rlo < shi && rhi > slo) inOwnStack = true;
        }
        if (readable && mbi.RegionSize >= 0x1000 && !inOwnStack) {
            unsigned char* base = (unsigned char*)mbi.BaseAddress;
            size_t size = mbi.RegionSize;
            const size_t CHUNK = 0x40000;
            for (size_t off = 0; off < size && found < maxOut; off += CHUNK) {
                size_t chunk = (size - off < CHUNK) ? (size - off) : CHUNK;
                unsigned char* p = base + off;
                __try {
                    for (size_t i = 0; i + 8 <= chunk && found < maxOut; i += 8) {
                        if (*(unsigned long long*)(p + i) == value) {
                            // 回读校验：排除扫描过程中内存被改写造成的误报
                            unsigned long long v2 = *(unsigned long long*)(p + i);
                            if (v2 == value) {
                                outAddrs[found] = (unsigned long long)(p + i);
                                found++;
                            }
                        }
                    }
                }
                __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            }
        }
        unsigned char* next = (unsigned char*)mbi.BaseAddress + mbi.RegionSize;
        if (next <= addr) break;
        addr = next;
    }
    return found;
}
// 安全内存读取（读失败返回非 0，不抛异常）
extern "C" __declspec(dllexport) int WxMemRead(unsigned long long addr, void* out, unsigned long long size)
{
    if (!out || addr < 0x10000 || size == 0 || size > 4096) return -1;
    __try { memcpy(out, (void*)addr, (size_t)size); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return -2; }
    return 0;
}
// 找持有指定指针的对象：扫描 qword==target 的位置，向后回溯找模块内指针（虚表候选）
extern "C" __declspec(dllexport) int WxFindOwner(unsigned long long target, unsigned long long moduleBase,
                                                unsigned long long moduleSize, unsigned long long* outRefs,
                                                unsigned long long* outVtables, int* outDelays, int maxOut)
{
    int found = 0;
    unsigned char* addr = nullptr;
    MEMORY_BASIC_INFORMATION mbi;
    while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi) && found < maxOut) {
        bool readable = (mbi.State == MEM_COMMIT) &&
            (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                            PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) &&
            !(mbi.Protect & PAGE_GUARD);
        unsigned long long slo = 0, shi = 0;
        GetCurStackLimits(&slo, &shi);
        bool inOwnStack = false;
        {
            unsigned long long rlo = (unsigned long long)mbi.BaseAddress;
            unsigned long long rhi = rlo + mbi.RegionSize;
            if (shi > slo && rlo < shi && rhi > slo) inOwnStack = true;
        }
        if (readable && mbi.RegionSize >= 0x1000 && !inOwnStack) {
            unsigned char* base = (unsigned char*)mbi.BaseAddress;
            size_t size = mbi.RegionSize;
            const size_t CHUNK = 0x40000;
            for (size_t off = 0; off < size && found < maxOut; off += CHUNK) {
                size_t chunk = (size - off < CHUNK) ? (size - off) : CHUNK;
                unsigned char* p = base + off;
                __try {
                    for (size_t i = 0; i + 8 <= chunk && found < maxOut; i += 8) {
                        if (*(unsigned long long*)(p + i) != target) continue;
                        unsigned long long refAddr = (unsigned long long)(p + i);
                        // 向前回溯 0x200 字节找模块内指针
                        unsigned long long vt = 0; int delay = -1;
                        for (int d = 0; d <= 0x200 && (size_t)i + 8 >= (size_t)d + 0; d += 8) {
                            if ((size_t)i < (size_t)d) break;
                            unsigned long long cand = *(unsigned long long*)(p + i - d);
                            if (cand >= moduleBase && cand < moduleBase + moduleSize) { vt = cand; delay = d; break; }
                        }
                        outRefs[found] = refAddr;
                        outVtables[found] = vt;
                        outDelays[found] = delay;
                        found++;
                    }
                }
                __except (EXCEPTION_EXECUTE_HANDLER) { break; }
            }
        }
        unsigned char* next = (unsigned char*)mbi.BaseAddress + mbi.RegionSize;
        if (next <= addr) break;
        addr = next;
    }
    return found;
}
// 安全内存写入（自动临时放开写保护）
extern "C" __declspec(dllexport) int WxMemWrite(unsigned long long addr, const void* src, unsigned long long size)
{
    if (!src || addr < 0x10000 || size == 0 || size > 1024) return -1;
    DWORD oldProt = 0;
    if (!VirtualProtect((void*)addr, (size_t)size, PAGE_READWRITE, &oldProt)) return -2;
    __try { memcpy((void*)addr, src, (size_t)size); }
    __except (EXCEPTION_EXECUTE_HANDLER) { VirtualProtect((void*)addr, (size_t)size, oldProt, &oldProt); return -3; }
    DWORD tmp = 0;
    VirtualProtect((void*)addr, (size_t)size, oldProt, &tmp);
    return 0;
}
// 受控调用 0x1C11050（需先注入并 hook 成功）
extern "C" __declspec(dllexport) int WxCallMgr(unsigned long long a1, unsigned long long a2,
                                              unsigned long long a3, unsigned long long a4)
{
    return sendprobe::CallMgrImpl((uint64_t)a1, (uint64_t)a2, (uint64_t)a3, (uint64_t)a4);
}
// 把"发送"任务投递到微信 UI 主线程执行（零人工触发）
extern "C" __declspec(dllexport) int WxDispatchSend()
{
    if (!sendprobe::g_wxHwnd || !sendprobe::g_origWndProc) return -1;
    if (!sendprobe::g_sendArgs[0]) return -2;   // 尚未捕获真实发送参数
    InterlockedExchange(&sendprobe::g_pendingSend, 1);
    DWORD_PTR res = 0;
    // 同步投递：阻塞到 UI 线程处理完毕，确保任务确实被执行
    LRESULT rr = SendMessageTimeoutW(sendprobe::g_wxHwnd, WM_NULL, 0, 0,
                                     SMTO_ABORTIFHUNG | SMTO_BLOCK, 1000, &res);
    if (rr == 0) return -3;
    return 0;
    return 0;
}

// ===================== 全自动发送：定位编辑器 + 写文本 + 投递发送 =====================
namespace autosend {

    static bool ValidateEditor(uint64_t obj)
    {
        __try {
            uint64_t sz  = *(uint64_t*)(obj + 0x1268 + 0x10);
            uint64_t cap = *(uint64_t*)(obj + 0x1268 + 0x18);
            if (cap != 15 || sz > 15) return false;
            // 文本区必须是可打印 ASCII 或 0
            const unsigned char* p = (const unsigned char*)(obj + 0x1268);
            for (int i = 0; i < 15; ++i) {
                unsigned char c = p[i];
                if (c != 0 && (c < 32 || c > 126)) return false;
            }
            return true;
        }
        __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
    }

    static bool FindEditor(uint64_t* out)
    {
        uintptr_t base = (uintptr_t)g_hWeixinDll;
        uint64_t vt = (uint64_t)base + 0x9515658ULL;   // 编辑器对象虚表 RVA
        unsigned char* addr = nullptr;
        MEMORY_BASIC_INFORMATION mbi;
        while (VirtualQuery(addr, &mbi, sizeof(mbi)) == sizeof(mbi)) {
            bool readable = (mbi.State == MEM_COMMIT) &&
                (mbi.Protect & (PAGE_READONLY | PAGE_READWRITE | PAGE_WRITECOPY |
                                PAGE_EXECUTE_READ | PAGE_EXECUTE_READWRITE | PAGE_EXECUTE_WRITECOPY)) &&
                !(mbi.Protect & PAGE_GUARD);
            if (readable && mbi.RegionSize >= 0x1000) {
                unsigned char* p = (unsigned char*)mbi.BaseAddress;
                size_t size = mbi.RegionSize;
                size_t found = 0;
                __try {
                    for (size_t i = 0; i + 8 <= size; i += 8) {
                        if (*(uint64_t*)(p + i) == vt) {
                            uint64_t obj = (uint64_t)(p + i);
                            if (ValidateEditor(obj)) { *out = obj; return true; }
                            if (++found > 64) break;
                        }
                    }
                }
                __except (EXCEPTION_EXECUTE_HANDLER) {}
            }
            unsigned char* next = (unsigned char*)mbi.BaseAddress + mbi.RegionSize;
            if (next <= addr) break;
            addr = next;
        }
        return false;
    }
}

extern "C" __declspec(dllexport) int WxFindEditor(unsigned long long* out)
{
    uint64_t e = 0;
    if (!autosend::FindEditor(&e)) return -1;
    if (out) *out = e;
    return 0;
}

// 全自动发送（文本 <= 15 字节，SSO 内联；发送到当前打开的聊天）
extern "C" __declspec(dllexport) int WxAutoSend(const char* text)
{
    if (!text) return -1;
    size_t len = strlen(text);
    if (len > 15) return -11;
    uint64_t editor = 0;
    if (!autosend::FindEditor(&editor)) return -10;
    unsigned char buf[16] = {0};
    memcpy(buf, text, len);
    if (WxMemWrite(editor + 0x1268, buf, 16) != 0) return -12;         // 文本缓冲
    uint64_t sz = (uint64_t)len;
    if (WxMemWrite(editor + 0x1268 + 0x10, &sz, 8) != 0) return -13;   // size
    return WxDispatchSend();
}
