#include "httplib.h"
#include "json.hpp"
#include <windows.h>
#include "wx_send.h"
#include "SendTextMsg.h"
#include "hook_log.h"

extern "C" int WxReplaySendText(const char* wxid, const char* text);
extern "C" int WxMemSearch(const char* needle, unsigned long long* outAddrs, int maxOut, int* outTypes, unsigned long long* outRegionBases);
extern "C" int WxMemSearchPtr(unsigned long long value, unsigned long long* outAddrs, int maxOut);
extern "C" int WxMemRead(unsigned long long addr, void* out, unsigned long long size);
extern "C" int WxMemWrite(unsigned long long addr, const void* src, unsigned long long size);
extern "C" int WxCallMgr(unsigned long long a1, unsigned long long a2, unsigned long long a3, unsigned long long a4);
extern "C" int WxDispatchSend();
extern "C" int WxAutoSend(const char* text);
extern "C" int WxFindEditor(unsigned long long* out);
extern "C" int WxFindOwner(unsigned long long target, unsigned long long moduleBase, unsigned long long moduleSize,
                           unsigned long long* outRefs, unsigned long long* outVtables, int* outDelays, int maxOut);
extern "C" int WxHookVtable(unsigned long long vtAddr, int n);
extern "C" int WxDynReset();
extern "C" int WxGetOutCands(unsigned long long* out, int maxOut);
extern "C" int WxSetInputInternal(const char* utf8Text, char* outBefore, int capBefore,
                                  char* outAfter, int capAfter, unsigned long long* outObj, int* outVisited);

using json = nlohmann::json;

void Route_SendTextMsg(httplib::Server& svr)
{
    svr.Post("/SendTextMsg", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson;
            json resp;

            try
            {
                reqJson = json::parse(req.body);
            }
            catch (...)
            {
                resp["ret"] = -1;
                resp["msg"] = "invalid json";
                res.set_content(resp.dump(), "application/json");
                return;
            }

            std::string wxidorgid = reqJson.value("wxidorgid", "");
            std::string msg = reqJson.value("msg", "");

            WeixinSend::SendText(wxidorgid, msg);


            resp["ret"] = 0;
            resp["retmsg"] = "success";

            res.set_content(resp.dump(), "application/json");
        });

    svr.Post("/Decode_Pic", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson;
            json resp;

            try
            {
                reqJson = json::parse(req.body);
            }
            catch (...)
            {
                resp["ret"] = -1;
                resp["msg"] = "invalid json";
                res.set_content(resp.dump(), "application/json");
                return;
            }
			

            std::string src_path = reqJson.value("src_path", "");
            std::string dst_path = reqJson.value("dst_path", "");

            OutputDebugStringA(("Decode_Pic src_path: " + src_path + "\n").c_str());
            OutputDebugStringA(("Decode_Pic dst_path: " + dst_path + "\n").c_str());


            WeixinSend::DecodePic(src_path, dst_path);


            resp["ret"] = 0;
            resp["retmsg"] = "success";

            res.set_content(resp.dump(), "application/json");
        });

    svr.Post("/SendReplay", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); }
            catch (...) { resp["ret"] = -1; resp["msg"] = "invalid json"; res.set_content(resp.dump(), "application/json"); return; }
            std::string wxid = reqJson.value("wxid", "");
            std::string text = reqJson.value("msg", "");
            int rc = WxReplaySendText(wxid.c_str(), text.c_str());
            resp["ret"] = rc;
            resp["msg"] = (rc == 0) ? "replayed" : "failed";
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/MemSearch", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); }
            catch (...) { resp["ret"] = -1; resp["msg"] = "invalid json"; res.set_content(resp.dump(), "application/json"); return; }
            std::string needle = reqJson.value("needle", "");
            const int MAX = 200;
            unsigned long long addrs[MAX] = {0};
            int types[MAX] = {0};
            unsigned long long regs[MAX] = {0};
            int n = WxMemSearch(needle.c_str(), addrs, MAX, types, regs);
            resp["ret"] = n;
            json hits = json::array();
            for (int i = 0; i < n && i < 40; ++i) {
                char buf[64];
                snprintf(buf, sizeof(buf), "%llX", addrs[i]);
                json h;
                h["addr"] = buf;
                h["type"] = (types[i] == 2) ? "utf16" : "utf8";
                char rbuf[64];
                snprintf(rbuf, sizeof(rbuf), "%llX", regs[i]);
                h["region"] = rbuf;
                hits.push_back(h);
            }
            resp["hits"] = hits;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/MemSearchPtr", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); }
            catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            std::string vs = reqJson.value("value", "");
            unsigned long long v = 0;
            try { v = std::stoull(vs, nullptr, 16); } catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            const int MAX = 200;
            unsigned long long addrs[MAX] = {0};
            int n = WxMemSearchPtr(v, addrs, MAX);
            resp["ret"] = n;
            json hits = json::array();
            for (int i = 0; i < n && i < 50; ++i) {
                char buf[64];
                snprintf(buf, sizeof(buf), "%llX", addrs[i]);
                hits.push_back(buf);
            }
            resp["hits"] = hits;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/MemRead", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); }
            catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            unsigned long long addr = 0, size = 0;
            try {
                addr = std::stoull(reqJson.value("addr", "0"), nullptr, 16);
                size = std::stoull(reqJson.value("size", "128"), nullptr, 10);
            } catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            if (size > 512) size = 512;
            unsigned char buf[520] = {0};
            int rc = WxMemRead(addr, buf, size);
            bool ok = (rc == 0);
            resp["ret"] = ok ? 0 : -3;
            json qs = json::array();
            if (ok) {
                for (unsigned long long i = 0; i + 8 <= size; i += 8) {
                    char b[64];
                    snprintf(b, sizeof(b), "%llX", *(unsigned long long*)(buf + i));
                    qs.push_back(b);
                }
            }
            resp["qwords"] = qs;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/FindOwner", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            unsigned long long target = 0, mb = 0, ms = 0;
            try {
                target = std::stoull(reqJson.value("target", "0"), nullptr, 16);
                mb = std::stoull(reqJson.value("base", "0"), nullptr, 16);
                ms = std::stoull(reqJson.value("size", "10000000"), nullptr, 16);
            } catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            const int MAX = 100;
            unsigned long long refs[MAX] = {0}, vts[MAX] = {0};
            int delays[MAX] = {0};
            int n = WxFindOwner(target, mb, ms, refs, vts, delays, MAX);
            resp["ret"] = n;
            json hits = json::array();
            for (int i = 0; i < n && i < 40; ++i) {
                json h;
                char b1[64], b2[64], b3[64];
                snprintf(b1, sizeof(b1), "%llX", refs[i]);
                snprintf(b2, sizeof(b2), "%llX", vts[i]);
                snprintf(b3, sizeof(b3), "%llX", (unsigned long long)(vts[i] ? vts[i] - mb : 0));
                h["ref"] = b1; h["vt"] = b2; h["vtRva"] = b3; h["delay"] = delays[i];
                hits.push_back(h);
            }
            resp["hits"] = hits;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/MemWrite", [](const httplib::Request& req, httplib::Response& res)
        {
            json reqJson; json resp;
            try { reqJson = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            unsigned long long addr = 0;
            std::string hex;
            try { addr = std::stoull(reqJson.value("addr", "0"), nullptr, 16); hex = reqJson.value("hex", ""); }
            catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            if (hex.size() % 2 != 0 || hex.empty() || hex.size() > 2048) { resp["ret"] = -3; res.set_content(resp.dump(), "application/json"); return; }
            unsigned char buf[1024] = {0};
            size_t n = hex.size() / 2;
            for (size_t i = 0; i < n; ++i) {
                int hi = 0, lo = 0;
                auto h = [](char c) -> int { if (c >= '0' && c <= '9') return c - '0'; if (c >= 'a' && c <= 'f') return c - 'a' + 10; if (c >= 'A' && c <= 'F') return c - 'A' + 10; return -1; };
                hi = h(hex[i*2]); lo = h(hex[i*2+1]);
                if (hi < 0 || lo < 0) { resp["ret"] = -4; res.set_content(resp.dump(), "application/json"); return; }
                buf[i] = (unsigned char)((hi << 4) | lo);
            }
            // 安全模式：只有当前内容与 expect 完全一致才写入（防止破坏未知结构）
            std::string expectHex = reqJson.value("expect", "");
            if (!expectHex.empty()) {
                size_t en = expectHex.size() / 2;
                unsigned char cur[1024] = {0};
                if (en > n || en > 1024) { resp["ret"] = -6; res.set_content(resp.dump(), "application/json"); return; }
                if (WxMemRead(addr, cur, en) != 0) { resp["ret"] = -7; res.set_content(resp.dump(), "application/json"); return; }
                for (size_t i = 0; i < en; ++i) {
                    int hi2 = 0, lo2 = 0;
                    auto hx = [](char c) -> int { if (c >= '0' && c <= '9') return c - '0'; if (c >= 'a' && c <= 'f') return c - 'a' + 10; if (c >= 'A' && c <= 'F') return c - 'A' + 10; return -1; };
                    hi2 = hx(expectHex[i*2]); lo2 = hx(expectHex[i*2+1]);
                    if ((unsigned char)((hi2 << 4) | lo2) != cur[i]) { resp["ret"] = -5; resp["msg"] = "expect mismatch"; res.set_content(resp.dump(), "application/json"); return; }
                }
            }
            int rc = WxMemWrite(addr, buf, n);
            resp["ret"] = rc;
            resp["written"] = (unsigned long long)n;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/CallMgr", [](const httplib::Request& req, httplib::Response& res)
        {
            json j; json resp;
            try { j = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            unsigned long long a1 = 0, a2 = 0, a3 = 0, a4 = 0;
            try {
                a1 = std::stoull(j.value("a1", "0"), nullptr, 16);
                a2 = std::stoull(j.value("a2", "0"), nullptr, 16);
                a3 = std::stoull(j.value("a3", "0"), nullptr, 16);
                a4 = std::stoull(j.value("a4", "0"), nullptr, 16);
            } catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            int rc = WxCallMgr(a1, a2, a3, a4);
            resp["ret"] = rc;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/DispatchSend", [](const httplib::Request& req, httplib::Response& res)
        {
            json resp;
            int rc = WxDispatchSend();
            resp["ret"] = rc;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/AutoSend", [](const httplib::Request& req, httplib::Response& res)
        {
            json j; json resp;
            try { j = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            std::string text = j.value("text", "");
            int rc = WxAutoSend(text.c_str());
            resp["ret"] = rc;
            resp["msg"] = (rc == 0) ? "ok" : (rc == -10 ? "editor not found" : (rc == -11 ? "text too long" : "failed"));
            res.set_content(resp.dump(), "application/json");
        });

    svr.Post("/FindEditor", [](const httplib::Request& req, httplib::Response& res)
        {
            unsigned long long e = 0;
            int rc = WxFindEditor(&e);
            json resp;
            resp["ret"] = rc;
            char b[64];
            snprintf(b, sizeof(b), "%llX", e);
            resp["editor"] = (rc == 0) ? b : "";
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/HookVt", [](const httplib::Request& req, httplib::Response& res)
        {
            json j; json resp;
            try { j = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            unsigned long long vt = 0; int n = 0;
            try {
                vt = std::stoull(j.value("addr", "0"), nullptr, 16);
                n = j.value("n", 8);
            } catch (...) { resp["ret"] = -2; res.set_content(resp.dump(), "application/json"); return; }
            int rc = WxHookVtable(vt, n);
            resp["ret"] = rc;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/DynReset", [](const httplib::Request& req, httplib::Response& res)
        {
            json resp;
            resp["ret"] = WxDynReset();
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/OutCands", [](const httplib::Request& req, httplib::Response& res)
        {
            const int MAX = 512;
            unsigned long long buf[MAX * 5] = {0};
            int n = WxGetOutCands(buf, MAX);
            json resp; json arr = json::array();
            resp["ret"] = n;
            for (int i = 0; i < n; ++i) {
                json h;
                char b[64];
                snprintf(b, sizeof(b), "%llX", buf[i*5+0]); h["val"] = b;
                snprintf(b, sizeof(b), "%llX", buf[i*5+1]); h["vt"] = b;
                snprintf(b, sizeof(b), "%llX", buf[i*5+2]); h["rva"] = b;
                snprintf(b, sizeof(b), "%llX", buf[i*5+3]); h["self"] = b;
                h["key"] = (int)(buf[i*5+4] >> 8);
                h["argi"] = (int)(buf[i*5+4] & 0xFF);
                arr.push_back(h);
            }
            resp["cands"] = arr;
            res.set_content(resp.dump(), "application/json");
        });
    svr.Post("/SetInputInternal", [](const httplib::Request& req, httplib::Response& res)
        {
            json j; json resp;
            try { j = json::parse(req.body); } catch (...) { resp["ret"] = -1; res.set_content(resp.dump(), "application/json"); return; }
            std::string text = j.value("text", "");
            char before[1024] = {0};
            char after[1024] = {0};
            unsigned long long obj = 0;
            int visited = 0;
            int rc = WxSetInputInternal(text.c_str(), before, sizeof(before), after, sizeof(after), &obj, &visited);
            resp["ret"] = rc;
            resp["before"] = before;
            resp["after"] = after;
            resp["visited"] = visited;
            char b[32];
            snprintf(b, sizeof(b), "%llX", obj);
            resp["obj"] = b;
            res.set_content(resp.dump(), "application/json");
        });
}
