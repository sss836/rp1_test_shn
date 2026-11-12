// Offline checks for the same LRO implementation used by the production binding.
#pragma once

#include "version_transport.hpp"

inline void lro_check(bool passed, const char* message) {
    if (!passed) throw std::runtime_error(message);
}

inline canfd_frame lro_feedback(uint16_t id, uint8_t header, uint8_t length) {
    canfd_frame frame{};
    frame.can_id = id;
    frame.len = length;
    frame.data[0] = header;
    frame.data[1] = frame.data[2] = frame.data[3] = frame.data[4] = frame.data[5] = 0xFF;
    frame.data[6] = 71;
    frame.data[7] = 60;
    frame.data[8] = 0x80;
    frame.data[10] = 0x40;
    return frame;
}

inline bool lro_native_feedback_checks() {
    auto bus = std::make_shared<VersionTestTransport>();
    LroMotorDriver motor(1, "canfd", "offline-profiles-feedback", LRO_6562, 0.25, bus);
    lro_check(motor.get_lro_protocol_profile() == "legacy", "LRO default profile changed");
    lro_check(!motor.get_motor_feedback_header() && !motor.get_motor_drive_enabled(), "initial feedback is fabricated");
    lro_check(!motor.set_lro_protocol_profile("109"), "firmware suffix selected a wire profile");

    const auto legacy = lro_feedback(1, 0x38, 12);
    bus->feed(legacy);
    lro_check(motor.get_feedback_count() == 1 && motor.get_error_id() == 0x18, "legacy fault bits changed");
    lro_check(std::abs(motor.get_motor_spd() - 21.89) < 0.001 && std::abs(motor.get_motor_current() - 35.3) < 0.001,
              "legacy velocity or torque changed");
    lro_check(!motor.get_motor_drive_enabled() && std::isnan(motor.get_motor_dc_bus_voltage()) &&
              std::isnan(motor.get_motor_iq_current()), "legacy feedback was labeled as V1.03");

    lro_check(motor.set_lro_protocol_profile("v1.03"), "explicit V1.03 profile not applied");
    lro_check(motor.get_feedback_count() == 1 && !motor.get_motor_feedback_header() && std::isnan(motor.get_motor_pos()),
              "switch reused decoded feedback or reset the monotonic counter");
    bus->feed(legacy);
    lro_check(motor.get_motor_drive_enabled() == true && motor.get_error_id() == 0, "enable/reserved bits were faults");
    lro_check(motor.get_motor_feedback_header() == 0x38 && motor.get_motor_spd() == 40.0f &&
              motor.get_motor_pos() == 12.75f && motor.get_motor_temperature() == 46.0f,
              "V1.03 feedback position, speed, or temperature mismatch");
    lro_check(motor.get_motor_iq_current() == 32.0f && std::isnan(motor.get_motor_current()), "amperes were exposed as torque");
    lro_check(std::abs(motor.get_motor_dc_bus_voltage() - 50.0f) < 0.002f &&
              std::abs(motor.get_motor_dc_bus_current() + 50.0f) < 0.002f, "DC bus scaling mismatch");
    lro_check(!motor.set_lro_protocol_profile("legacy"), "profile changed while drive reported enabled");

    auto invalid = lro_feedback(1, 0x20, 9);
    bus->feed(invalid);
    invalid.len = 12;
    invalid.can_id = 2;
    bus->feed(invalid);
    invalid.can_id = 1 | CAN_EFF_FLAG;
    bus->feed(invalid);
    invalid.can_id = 1 | CAN_RTR_FLAG;
    bus->feed(invalid);
    bus->feed(lro_feedback(1, 0x40, 8));
    lro_check(motor.get_feedback_count() == 2 && motor.get_motor_feedback_header() == 0x38,
              "invalid data updated feedback or freshness");

    bus->feed(lro_feedback(1, 0x37, 8));
    lro_check(motor.get_error_id() == 7 && motor.get_motor_drive_enabled() == true, "real V1.03 fault was masked");
    lro_check(std::isnan(motor.get_motor_dc_bus_voltage()) && std::isnan(motor.get_motor_dc_bus_current()),
              "short frame retained the previous DC bus values");
    bus->feed(lro_feedback(1, 0x28, 8));
    lro_check(motor.get_error_id() == 0 && motor.get_motor_drive_enabled() == false, "disabled state was not observed");
    lro_check(motor.set_lro_protocol_profile("legacy"), "disabled unused driver rejected profile selection");
    lro_check(std::isnan(motor.get_motor_iq_current()) && !motor.get_motor_feedback_header() &&
              motor.get_feedback_count() == 4, "switch back reused current or feedback age");
    return true;
}

inline bool lro_native_command_checks() {
    auto bus = std::make_shared<VersionTestTransport>();
    LroMotorDriver legacy(1, "canfd", "offline-profiles-command", LRO_6562, 0.25, bus);
    LroMotorDriver v103(2, "canfd", "offline-profiles-command", LRO_6562, 0.25, bus);
    LroMotorDriver outside_batch(17, "canfd", "offline-profiles-command", LRO_10062, 0.0, bus);
    lro_check(v103.set_lro_protocol_profile("v1.03"), "V1.03 setup failed");
    legacy.set_motor_control_mode(MotorDriver::MIT);
    v103.set_motor_control_mode(MotorDriver::MIT);
    legacy.motor_mit_cmd(1.25f, 10.0f, 20.0f, 2.0f, 5.0f);
    v103.motor_mit_cmd(1.25f, 10.0f, 20.0f, 2.0f, 5.0f);
    const auto legacy_mit = bus->sent[0];
    const auto v103_mit = bus->sent[1];
    // MIT velocity occupies bits 23..12 (data[5] and data[6] high nibble).
    const auto mit_speed = [](const canfd_frame& frame) { return (frame.data[5] << 4) | (frame.data[6] >> 4); };
    const auto mit_torque = [](const canfd_frame& frame) { return ((frame.data[6] & 0x0F) << 8) | frame.data[7]; };
    lro_check(mit_speed(legacy_mit) == 2982 && mit_speed(v103_mit) == 2559, "MIT profile did not select the speed range");
    lro_check(mit_torque(legacy_mit) == 2337 && mit_torque(v103_mit) == 2303, "MIT profile did not select the torque range");

    float positions[8] = {1.25f, 1.25f}, velocities[8] = {10.0f, 10.0f};
    float kp[8] = {20.0f, 20.0f}, kd[8] = {2.0f, 2.0f}, torque[8] = {5.0f, 5.0f};
    legacy.motor_mit_cmd(positions, velocities, kp, kd, torque);
    const auto batch = bus->sent.back();
    lro_check(batch.can_id == (0x8080 | CAN_EFF_FLAG) && batch.len == 64, "MIT batch transport changed");
    lro_check(memcmp(batch.data, legacy_mit.data, 8) == 0 && memcmp(batch.data + 8, v103_mit.data, 8) == 0,
              "MIT batch did not use each node's single-frame encoding");
    lro_check(outside_batch.get_response_count() == 0, "unsupported motor ID occupied a batch slot");

    const auto pos_current_limit = [](const canfd_frame& frame) {
        return ((frame.data[6] & 0x3F) << 6) | (frame.data[7] >> 2);
    };
    const auto speed_current_limit = [](const canfd_frame& frame) { return (frame.data[5] << 8) | frame.data[6]; };
    legacy.set_motor_control_mode(MotorDriver::POS);
    legacy.motor_pos_cmd(1.25f, 1.0f, false);
    lro_check(pos_current_limit(bus->sent.back()) == 4095, "legacy POS current limit changed");
    legacy.set_motor_control_mode(MotorDriver::SPD);
    legacy.motor_spd_cmd(1.0f);
    lro_check(speed_current_limit(bus->sent.back()) == 65535, "legacy SPD current limit changed");

    constexpr float expected_speed[] = {20.0f, 40.0f, 35.0f, 16.0f};
    constexpr float expected_current[] = {18.0f, 32.0f, 35.0f, 90.0f};
    for (int model = LRO_5550; model < LRO_Num_Of_Motor; ++model) {
        auto model_bus = std::make_shared<VersionTestTransport>();
        LroMotorDriver motor(3, "canfd", "offline-profiles-models", static_cast<LRO_Motor_Model>(model), 0, model_bus);
        lro_check(motor.set_lro_protocol_profile("v1.03"), "model profile setup failed");
        model_bus->feed(lro_feedback(3, 0x20, 8));
        lro_check(motor.get_motor_spd() == expected_speed[model] && motor.get_motor_iq_current() == expected_current[model],
                  "V1.03 model-specific ranges mismatch");
        motor.set_motor_control_mode(MotorDriver::POS);
        motor.motor_pos_cmd(1.25f, 1.0f, false);
        const int current_limit = static_cast<int>(expected_current[model] * 10);
        lro_check(pos_current_limit(model_bus->sent.back()) == current_limit, "V1.03 POS current limit mismatch");
        motor.set_motor_control_mode(MotorDriver::SPD);
        motor.motor_spd_cmd(1.0f);
        lro_check(speed_current_limit(model_bus->sent.back()) == current_limit, "V1.03 SPD current limit mismatch");
    }

    legacy.lock_motor();
    legacy.unlock_motor();
    lro_check(legacy.set_lro_protocol_profile("legacy") && !legacy.set_lro_protocol_profile("v1.03"),
              "enable attempt did not freeze profile for the handle lifetime");
    return true;
}

inline bool lro_native_zero_checks() {
    auto bus = std::make_shared<VersionTestTransport>();
    LroMotorDriver motor(2, "canfd", "offline-profiles-zero", LRO_6562, 0., bus);
    lro_check(motor.set_lro_protocol_profile("v1.03"), "V1.03 zero setup failed");
    lro_check(!motor.set_motor_zero() && !motor.request_lro_zero(), "marking started without known disabled feedback");
    bus->feed(lro_feedback(2, 0x30, 8));
    lro_check(!motor.request_lro_zero(), "marking started while enabled");
    bus->feed(lro_feedback(2, 0x27, 8));
    lro_check(!motor.request_lro_zero() && bus->sent.empty(), "marking started while faulted");
    bus->feed(lro_feedback(2, 0x20, 8));
    motor.lock_motor();
    lro_check(!motor.request_lro_zero(), "marking started after an unconfirmed enable attempt");
    motor.unlock_motor();
    lro_check(!motor.request_lro_zero(), "marking reused feedback preceding a disable command");
    bus->sent.clear();
    bus->feed(lro_feedback(2, 0x20, 8));
    bus->accept_checked_tx = false;
    lro_check(!motor.request_lro_zero() && bus->sent.empty(), "a rejected transport queue was reported as submitted");
    bus->accept_checked_tx = true;
    lro_check(motor.request_lro_zero(), "disabled V1.03 marking not queued");
    const auto& frame = bus->sent.back();
    lro_check(bus->sent.size() == 1 && frame.can_id == 0x7FF && frame.len == 4 &&
              frame.data[0] == 0 && frame.data[1] == 2 && frame.data[2] == 0 && frame.data[3] == 0x03,
              "V1.03 marking used the wrong command or target");
    lro_check(!motor.request_lro_zero() && !motor.set_lro_protocol_profile("legacy"), "pending marking can be repeated or bypassed");
    bool rejected_enable = false;
    try {
        motor.lock_motor();
    } catch (const std::runtime_error&) {
        rejected_enable = true;
    }
    lro_check(rejected_enable && bus->sent.size() == 1, "enable was sent while marking remained pending");
    return true;
}
