// Offline CAN-FD transport. Compiled only into motors_py_testing.
#pragma once
#include "drivers/lro/lro_motor_driver.hpp"
#include <stdexcept>

class VersionTestTransport final : public MotorsCANFD {
public:
    CanFdCbkMap callbacks;
    std::vector<canfd_frame> sent;
    bool automatic = false;
    bool accept_checked_tx = true;
    void transmit(const canfd_frame& frame) override {
        sent.push_back(frame);
        if (automatic && frame.len == 2 && frame.data[0] == 0xE0) {
            canfd_frame reply{};
            reply.can_id = frame.can_id;
            reply.data[0] = 0xA0; reply.data[1] = frame.data[1];
            if (frame.data[1] == 0x31) {
                reply.len = 20;
                memcpy(reply.data + 2, "1.0.8", 5);
            } else {
                reply.len = 12;
                const uint8_t version[] = {1,0,8,1,0,2,1,0,0,0};
                memcpy(reply.data + 2, version, 10);
            }
            feed(reply);
        }
    }
    bool try_transmit(const canfd_frame& frame) override {
        if (!accept_checked_tx) return false;
        transmit(frame);
        return true;
    }
    void feed(const canfd_frame& frame) {
        // Deliver deliberately misrouted frames too, to exercise driver guard.
        for (auto& pair : callbacks) pair.second(frame);
    }
    void add_canfd_callback(const CanFdCbkFunc& fn, CanFdCbkId id) override { callbacks[id] = fn; }
    void remove_canfd_callback(CanFdCbkId id) override { callbacks.erase(id); }
    void clear_canfd_callbacks() override { callbacks.clear(); }
    void set_canfd_key_extractor(CanFdCbkKeyExtractor) override {}
};

inline std::shared_ptr<MotorDriver> version_test_motor() {
    auto bus = std::make_shared<VersionTestTransport>();
    bus->automatic = true;
    return std::make_shared<LroMotorDriver>(3, "canfd", "offline-versions", LRO_5550, 0., bus);
}

inline bool version_native_checks() {
    auto check = [](bool ok) { if (!ok) throw std::runtime_error("native version check failed"); };
    auto bus = std::make_shared<VersionTestTransport>();
    auto motor = std::make_shared<LroMotorDriver>(3, "canfd", "offline-checks", LRO_5550, 0., bus);
    check(!motor->request_version(0x03));
    check(motor->request_version(0x31));
    check(bus->sent.size() == 1 && bus->sent[0].can_id == 3 && bus->sent[0].len == 2 && bus->sent[0].data[0] == 0xE0);
    auto before = motor->get_motor_pos();
    canfd_frame reply{};
    reply.len = 20; reply.can_id = 4; reply.data[0] = 0xA0; reply.data[1] = 0x31;
    memcpy(reply.data + 2, "1.0.8", 5);
    bus->feed(reply); check(motor->version_payload(0x31).empty());
    reply.can_id = 3 | CAN_EFF_FLAG;
    bus->feed(reply); check(motor->version_payload(0x31).empty());
    reply.can_id = 3; reply.data[1] = 0x1E;
    bus->feed(reply); check(motor->version_payload(0x31).empty());
    reply.data[1] = 0x31;
    bus->feed(reply); check(motor->version_payload(0x31).size() == 20);
    check(motor->get_motor_pos() == before && motor->get_feedback_count() == 0);
    motor->request_version(0x31); check(motor->version_payload(0x31).empty());
    std::this_thread::sleep_for(std::chrono::milliseconds(510));
    bus->feed(reply); check(motor->version_payload(0x31).empty());
    motor->request_version(0x1E);
    reply.data[1] = 0x31; bus->feed(reply); check(motor->version_payload(0x31).empty());
    return true;
}
