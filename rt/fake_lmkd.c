#include <sys/types.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <android/log.h>

#define TAG "FakeLmkd"

static void* client_thread(void* arg) {
    int fd = (int)(intptr_t)arg;
    char buf[8192];
    for (;;) {
        ssize_t n = read(fd, buf, sizeof(buf));
        if (n <= 0) break;
    }
    close(fd);
    return NULL;
}

int main(void) {
    int fd = -1;
    const char* env = getenv("ANDROID_SOCKET_lmkd");
    if (env != NULL && env[0] != '\0') {
        fd = atoi(env);
        __android_log_print(ANDROID_LOG_INFO, TAG, "using init socket fd=%d", fd);
        if (listen(fd, 8) < 0) {
            __android_log_print(ANDROID_LOG_WARN, TAG, "listen(init fd): %s", strerror(errno));
        }
    }
    if (fd < 0) {
        fd = socket(AF_UNIX, SOCK_SEQPACKET, 0);
        if (fd < 0) { __android_log_print(ANDROID_LOG_ERROR, TAG, "socket: %s", strerror(errno)); return 1; }
        struct sockaddr_un addr;
        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        strcpy(addr.sun_path, "/dev/socket/lmkd");
        unlink(addr.sun_path);
        if (bind(fd, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
            __android_log_print(ANDROID_LOG_ERROR, TAG, "bind: %s", strerror(errno));
            return 1;
        }
        if (listen(fd, 8) < 0) {
            __android_log_print(ANDROID_LOG_ERROR, TAG, "listen: %s", strerror(errno));
            return 1;
        }
        __android_log_print(ANDROID_LOG_INFO, TAG, "listening on /dev/socket/lmkd fd=%d", fd);
    }
    for (;;) {
        int c = accept(fd, NULL, NULL);
        if (c < 0) {
            if (errno == EINTR) continue;
            __android_log_print(ANDROID_LOG_WARN, TAG, "accept: %s", strerror(errno));
            usleep(200000);
            continue;
        }
        __android_log_print(ANDROID_LOG_INFO, TAG, "client connected fd=%d", c);
        pthread_t th;
        if (pthread_create(&th, NULL, client_thread, (void*)(intptr_t)c) == 0) {
            pthread_detach(th);
        } else {
            close(c);
        }
    }
    return 0;
}