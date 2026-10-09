/* Real Win32 probe; built with the Windows SDK in the opt-in kernel suite.
   Static CRT avoids granting a development Python or compiler installation. */
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <wchar.h>

static DWORD file_access(const wchar_t *path, DWORD access, DWORD disposition) {
    HANDLE h = CreateFileW(path, access, FILE_SHARE_READ | FILE_SHARE_WRITE |
                          FILE_SHARE_DELETE, NULL, disposition, 0, NULL);
    if (h == INVALID_HANDLE_VALUE) return GetLastError();
    CloseHandle(h);
    return 0;
}

static BOOL child(const wchar_t *mode, DWORD flags, PROCESS_INFORMATION *pi) {
    wchar_t exe[32768], line[32768];
    STARTUPINFOW si = {0};
    GetModuleFileNameW(NULL, exe, 32768);
    _snwprintf_s(line, 32768, _TRUNCATE, L"\"%ls\" %ls", exe, mode);
    si.cb = sizeof(si);
    si.dwFlags = STARTF_USESTDHANDLES;
    si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);
    si.hStdOutput = GetStdHandle(STD_OUTPUT_HANDLE);
    si.hStdError = GetStdHandle(STD_ERROR_HANDLE);
    return CreateProcessW(exe, line, NULL, NULL, TRUE, flags | CREATE_NO_WINDOW,
                          NULL, NULL, &si, pi);
}

int wmain(int argc, wchar_t **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);
    if (argc < 2) return 90;
    if (!wcscmp(argv[1], L"sleep")) {
        printf("{\"ready\":%lu}\n", GetCurrentProcessId());
        Sleep(60000);
        return 0;
    }
    if (!wcscmp(argv[1], L"tree") || !wcscmp(argv[1], L"middle")) {
        PROCESS_INFORMATION pi = {0};
        BOOL middle = !wcscmp(argv[1], L"middle");
        if (!child(middle ? L"sleep" : L"middle", 0, &pi)) return 91;
        printf("{\"child\":%lu}\n", pi.dwProcessId);
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
        Sleep(middle ? 60000 : 500);
        return 0;
    }
    if (!wcscmp(argv[1], L"breakaway")) {
        PROCESS_INFORMATION pi = {0};
        if (child(L"sleep", CREATE_BREAKAWAY_FROM_JOB, &pi)) {
            /* Avoid leaking a test child if a regression permits escape. */
            TerminateProcess(pi.hProcess, 1);
            WaitForSingleObject(pi.hProcess, 5000);
            CloseHandle(pi.hThread);
            CloseHandle(pi.hProcess);
            return 92;
        }
        printf("{\"breakaway_error\":%lu}\n", GetLastError());
        return 0;
    }
    if (!wcscmp(argv[1], L"flood")) {
        for (int i = 0; i < 100000; ++i) {
            fputs("stdout-payload\n", stdout);
            fputs("stderr-payload\n", stderr);
        }
        return 0;
    }
    if (!wcscmp(argv[1], L"probe") && argc == 7) {
        DWORD container = 0, size = 0, caps = 999;
        BOOL job = FALSE;
        HANDLE token = NULL;
        if (!OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token)) return 93;
        if (!GetTokenInformation(token, TokenIsAppContainer, &container,
                                  sizeof(container), &size)) return 94;
        GetTokenInformation(token, TokenCapabilities, NULL, 0, &size);
        TOKEN_GROUPS *groups = (TOKEN_GROUPS *)malloc(size);
        if (!groups || !GetTokenInformation(token, TokenCapabilities, groups, size, &size))
            return 95;
        caps = groups->GroupCount;
        free(groups);
        CloseHandle(token);
        IsProcessInJob(GetCurrentProcess(), NULL, &job);
        WSADATA data;
        if (WSAStartup(MAKEWORD(2, 2), &data)) return 96;
        SOCKET s = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        int network = WSAGetLastError();
        if (s != INVALID_SOCKET) {
            struct sockaddr_in address = {0};
            address.sin_family = AF_INET;
            address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            address.sin_port = htons((u_short)_wtoi(argv[6]));
            network = connect(s, (struct sockaddr *)&address, sizeof(address)) == 0
                          ? 0 : WSAGetLastError();
            closesocket(s);
        }
        WSACleanup();
        printf("{\"container\":%lu,\"capabilities\":%lu,\"job\":%d,"
               "\"read\":%lu,\"write\":%lu,\"all_packages_read\":%lu,"
               "\"scratch\":%lu,\"network\":%d}\n",
               container, caps, job,
               file_access(argv[2], GENERIC_READ, OPEN_EXISTING),
               file_access(argv[3], GENERIC_WRITE, CREATE_ALWAYS),
               file_access(argv[4], GENERIC_READ, OPEN_EXISTING),
               file_access(argv[5], GENERIC_WRITE, CREATE_ALWAYS), network);
        return 0;
    }
    return 97;
}
