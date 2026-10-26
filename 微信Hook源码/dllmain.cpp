// dllmain.cpp
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <string>
#include "tools.h"
#include "version_proxy.h"
#include "hideself.h"
#include "global.h"
#include "hook_log.h"

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
