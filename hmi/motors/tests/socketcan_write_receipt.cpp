// Link the production TX worker to an AF_UNIX socket pair and inject write outcomes.
#include "socket_canfd.hpp"

#include <atomic>
#include <chrono>
#include <cstdarg>
#include <cstdlib>
#include <system_error>

using namespace std::chrono_literals;

namespace {
int local_fd = -1;
int peer_fd = -1;
std::atomic<int> rejected{0};
std::atomic<int> accepted{0};
}

extern "C" int __wrap_socket(int domain, int kind, int protocol) {
    if (domain != PF_CAN || kind != SOCK_RAW || protocol != CAN_RAW) std::abort();
    int pair[2];
    if (::socketpair(AF_UNIX, SOCK_DGRAM, 0, pair)) std::abort();
    local_fd = pair[0];
    peer_fd = pair[1];
    return local_fd;
}

extern "C" int __wrap_bind(int fd, const sockaddr*, socklen_t) {
    if (fd != local_fd) std::abort();
    return 0;
}

extern "C" int __wrap_ioctl(int fd, unsigned long request, ...) {
    if (fd != local_fd || request != SIOCGIFINDEX) std::abort();
    va_list args;
    va_start(args, request);
    va_arg(args, ifreq*)->ifr_ifindex = 1;
    va_end(args);
    return 0;
}

extern "C" int __real_setsockopt(int, int, int, const void*, socklen_t);
extern "C" int __wrap_setsockopt(int fd, int level, int name, const void* data, socklen_t len) {
    if (fd == local_fd && level == SOL_CAN_RAW) return 0;
    return __real_setsockopt(fd, level, name, data, len);
}

extern "C" ssize_t __real_write(int, const void*, size_t);
extern "C" ssize_t __wrap_write(int fd, const void* data, size_t size) {
    if (fd == local_fd && size == sizeof(canfd_frame)) {
        if (static_cast<const canfd_frame*>(data)->data[0] == 1) {
            ++rejected;
            errno = ENOBUFS;
            return -1;
        }
        ++accepted;
    }
    return __real_write(fd, data, size);
}

int main() {
    auto bus = std::dynamic_pointer_cast<MotorsSocketCANFD>(MotorsCANFD::get("mockcan0"));
    canfd_frame frame{};
    frame.can_id = 0x8080 | CAN_EFF_FLAG;
    frame.len = 64;
    frame.data[0] = 1;
    try {
        bus->transmit_checked(frame);
        throw std::runtime_error("a rejected frame completed successfully");
    } catch (const std::system_error& exc) {
        if (exc.code().value() != ENOBUFS ||
            std::string(exc.what()).find("mockcan0 CAN ID 0x8080") == std::string::npos) throw;
    }
    bus->set_send_sleep(120000);
    frame.data[0] = 2;
    bus->transmit_checked(frame);
    frame.data[0] = 3;
    try {
        bus->transmit_checked(frame);
        throw std::runtime_error("a queued frame survived its deadline");
    } catch (const std::system_error& exc) {
        if (exc.code().value() != ETIMEDOUT) throw;
    }
    // The worker now wakes up and drains the expired command.
    std::this_thread::sleep_for(150ms);
    bus.reset();
    ::close(peer_fd);
    if (rejected != FD_MAX_RETRY_COUNT + 1 || accepted != 1) {
        throw std::runtime_error("incorrect retry count or expired frame reached the socket");
    }
    std::puts("simulated CAN-FD: checked write, ENOBUFS, timeout cancellation passed");
}
