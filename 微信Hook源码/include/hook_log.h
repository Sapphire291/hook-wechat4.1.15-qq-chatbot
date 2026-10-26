#pragma once
#include <windows.h>
#include <cstdio>
#include <cstdarg>

inline void HookLog(const char* fmt, ...)
{
    char buf[2048] = {0};
    va_list ap; va_start(ap, fmt);
    _vsnprintf_s(buf, sizeof(buf) - 2, _TRUNCATE, fmt, ap);
    va_end(ap);

    SYSTEMTIME st; GetLocalTime(&st);
    // 日志写到系统临时目录，避免绑定固定盘符（原实现写死固定盘符路径）
    wchar_t logDir[MAX_PATH] = {0};
    wchar_t logPath[MAX_PATH] = {0};
    if (GetTempPathW(MAX_PATH, logDir) > 0)
        _snwprintf_s(logPath, MAX_PATH, _TRUNCATE, L"%sChatBot_hook_log.txt", logDir);
    FILE* f = nullptr;
    if (_wfopen_s(&f, logPath[0] ? logPath : L"hook_log.txt", L"a, ccs=UTF-8") == 0 && f)
    {
        fwprintf(f, L"[%02d:%02d:%02d.%03d] %hs\n", st.wHour, st.wMinute, st.wSecond, st.wMilliseconds, buf);
        fclose(f);
    }
    // 不再使用 OutputDebugStringA：它会触发 0x40010006 异常，可能被 VEH 再次捕获
}