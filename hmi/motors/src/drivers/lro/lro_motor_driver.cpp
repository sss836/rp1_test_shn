// SPDX-License-Identifier: GPL-3.0
// Copyright (C) 2026 wentywenty
// Copyright (C) 2026 Luo1imasi

#include "lro_motor_driver.hpp"

namespace {
constexpr LRO_Limit_Param legacy_limits[LRO_Num_Of_Motor] = {
    {12.5, 21.0, 36.0, 0.0, 500.0, 50.0},
    {12.5, 21.89, 35.3, 0.0, 500.0, 50.0},
    {12.5, 16.13, 56.5, 0.0, 500.0, 50.0},
    {12.5, 14.24, 141.7, 0.0, 500.0, 50.0},
};

// roboparty_deploy 95be350 pins roboparty_motors a5c930a. V1.03 wire encoding ranges.
constexpr LRO_Limit_Param v103_limits[LRO_Num_Of_Motor] = {
    {12.5, 20.0, 30.0, 18.0, 500.0, 50.0},
    {12.5, 40.0, 40.0, 32.0, 500.0, 50.0},
    {12.5, 35.0, 65.0, 35.0, 500.0, 50.0},
    {12.5, 16.0, 165.0, 90.0, 500.0, 50.0},
};
constexpr float missing = std::numeric_limits<float>::quiet_NaN();
constexpr int missing_header = -1;
constexpr uint8_t v103_drive_enabled = 0x10;
}

LroMotorDriver::LroMotorDriver(uint16_t motor_id, const std::string& interface_type, const std::string& can_interface,
                               LRO_Motor_Model motor_model, double motor_zero_offset,
                               std::shared_ptr<MotorsCANFD> transport)
    : MotorDriver(), motor_model_(motor_model) {
    if (interface_type != "canfd" && interface_type != "ethercanfd" && interface_type != "ethercat") {
        throw std::runtime_error("LRO driver only supports CAN-FD and Ethercat interfaces");
    }
    if (motor_model_ < LRO_5550 || motor_model_ >= LRO_Num_Of_Motor) {
        throw std::invalid_argument("Unsupported LRO motor model");
    }
    motor_id_ = motor_id;
    can_interface_ = can_interface;
    motor_zero_offset_ = motor_zero_offset;

    if (interface_type == "canfd" || interface_type == "ethercanfd") {
        comm_type_ = CommType::CANFD;
        if (motor_id_ > 0 && motor_id_ <= 8) motor_index_ = motor_id_ - 1;
        canfd_ = transport ? transport : MotorsCANFD::get(can_interface);

        CanFdCbkFunc canfd_callback = std::bind(&LroMotorDriver::canfd_rx_cbk, this, std::placeholders::_1);
        canfd_->add_canfd_callback(canfd_callback, motor_id_);
        std::lock_guard<std::shared_mutex> lock(bus_registry_mutex_);
        bus_registry_[can_interface].push_back(this);
    } else if (interface_type == "ethercat") {
        comm_type_ = CommType::ETHERCAT;
        throw std::runtime_error("LRO driver does not support EtherCAT interface yet");
    }
}

LroMotorDriver::~LroMotorDriver() {
    if (comm_type_ == CommType::CANFD) {
        canfd_->remove_canfd_callback(motor_id_);
        std::lock_guard<std::shared_mutex> lock(bus_registry_mutex_);
        auto it = bus_registry_.find(can_interface_);
        if (it != bus_registry_.end()) {
            auto& motors = it->second;
            motors.erase(std::remove(motors.begin(), motors.end(), this), motors.end());
            if (motors.empty()) {
                bus_registry_.erase(it);
            }
        }
    }
}

void LroMotorDriver::lock_motor() {
    if (comm_type_ == CommType::CANFD) {
        std::lock_guard<std::mutex> drive_guard(drive_control_mutex_);
        {
            std::lock_guard<std::mutex> guard(protocol_mutex_);
            if (zero_requested_) throw std::runtime_error("LRO zero request requires power cycle and a new driver handle");
            // Once an enable frame may have been sent, keep one encoder for
            // this handle even if the disable command has no acknowledgement.
            profile_frozen_ = true;
            enable_requested_ = true;
        }
        canfd_frame tx_frame{};
        tx_frame.can_id = 0x7FF;
        tx_frame.len = 0x04;
        tx_frame.flags = CANFD_BRS;

        tx_frame.data[0] = (motor_id_ >> 8) & 0xFF;
        tx_frame.data[1] = motor_id_ & 0xFF;
        tx_frame.data[2] = 0x00;
        tx_frame.data[3] = LRO_CMD_ENABLE;
        
        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

void LroMotorDriver::unlock_motor() {
    if (comm_type_ == CommType::CANFD) {
        std::lock_guard<std::mutex> drive_guard(drive_control_mutex_);
        canfd_frame tx_frame{};
        tx_frame.can_id = 0x7FF;
        tx_frame.len = 0x04;
        tx_frame.flags = CANFD_BRS;

        tx_frame.data[0] = (motor_id_ >> 8) & 0xFF;
        tx_frame.data[1] = motor_id_ & 0xFF;
        tx_frame.data[2] = 0x00;
        tx_frame.data[3] = LRO_CMD_DISABLE;

        if (protocol_profile_ == ProtocolProfile::V103) {
            if (canfd_->try_transmit(tx_frame)) {
                std::lock_guard<std::mutex> guard(protocol_mutex_);
                enable_requested_ = false;
                last_disable_queued_at_ = std::chrono::steady_clock::now();
            }
        } else {
            canfd_->transmit(tx_frame);
        }
    }
    {
        response_count_++;
    }
}

uint8_t LroMotorDriver::init_motor() {
    // send disable command to enter read mode
    LroMotorDriver::unlock_motor();
    Timer::sleep_for(normal_sleep_time);
    LroMotorDriver::set_motor_control_mode(MIT);
    Timer::sleep_for(normal_sleep_time);
    // send enable command to enter contorl mode
    LroMotorDriver::lock_motor();
    Timer::sleep_for(normal_sleep_time);
    LroMotorDriver::refresh_motor_status();
    Timer::sleep_for(normal_sleep_time);
    switch (error_id_) {
        case LROError::LRO_MOTOR_OVERHEAT:
            return LROError::LRO_MOTOR_OVERHEAT;
        case LROError::LRO_OVER_CURRENT:
            return LROError::LRO_OVER_CURRENT;
        case LROError::LRO_UNDER_VOLTAGE:
            return LROError::LRO_UNDER_VOLTAGE;
        case LROError::LRO_ENCODER_ERROR:
            return LROError::LRO_ENCODER_ERROR;
        case LROError::LRO_BRAKE_OVERVOLT:
            return LROError::LRO_BRAKE_OVERVOLT;
        case LROError::LRO_DRV_ERROR:
            return LROError::LRO_DRV_ERROR;
        default:
            return error_id_;
    }
    return error_id_;
}

void LroMotorDriver::deinit_motor() {
    LroMotorDriver::unlock_motor();
    Timer::sleep_for(normal_sleep_time);
}

bool LroMotorDriver::write_motor_flash() {
    return true;
}

bool LroMotorDriver::set_motor_zero() {
    {
        std::lock_guard<std::mutex> guard(protocol_mutex_);
        if (protocol_profile_ == ProtocolProfile::V103) return false;
        profile_frozen_ = true;
    }
    // send set zero command
    canfd_->transmit(zero_request_frame());
    response_count_++;
    Timer::sleep_for(setup_sleep_time);
    LroMotorDriver::refresh_motor_status();
    Timer::sleep_for(setup_sleep_time);
    logger_->info("motor_id: {0}\tposition: {1}", motor_id_, get_motor_pos());
    LroMotorDriver::unlock_motor();
    if (get_motor_pos() > judgment_accuracy_threshold || get_motor_pos() < -judgment_accuracy_threshold) {
        logger_->warn("set zero error");
        return false;
    } else {
        logger_->info("set zero success");
        return true;
    }
}

bool LroMotorDriver::request_lro_zero() {
    std::lock_guard<std::mutex> drive_guard(drive_control_mutex_);
    bool was_frozen;
    {
        std::lock_guard<std::mutex> guard(protocol_mutex_);
        const int header = feedback_header_;
        if (protocol_profile_ != ProtocolProfile::V103 || zero_requested_ || enable_requested_ || header == missing_header ||
            (header & (v103_drive_enabled | 0x07)) != 0 ||
            last_feedback_at_ < last_disable_queued_at_ ||
            std::chrono::steady_clock::now() - last_feedback_at_ > std::chrono::milliseconds(500)) {
            return false;
        }
        // Block re-enable and duplicate requests while queuing the command.
        was_frozen = profile_frozen_;
        zero_requested_ = true;
        profile_frozen_ = true;
    }
    if (canfd_->try_transmit(zero_request_frame())) {
        response_count_++;
        return true;
    }
    std::lock_guard<std::mutex> guard(protocol_mutex_);
    zero_requested_ = false;
    profile_frozen_ = was_frozen;
    return false;
}

const LRO_Limit_Param& LroMotorDriver::encoding_limits(ProtocolProfile profile) const {
    return profile == ProtocolProfile::V103 ? v103_limits[motor_model_] : legacy_limits[motor_model_];
}

bool LroMotorDriver::set_lro_protocol_profile(const std::string& profile) {
    ProtocolProfile requested;
    if (profile == "legacy") requested = ProtocolProfile::Legacy;
    else if (profile == "v1.03") requested = ProtocolProfile::V103;
    else return false;

    std::lock_guard<std::mutex> guard(protocol_mutex_);
    const auto current = protocol_profile_.load();
    if (requested == current) return true;
    const int header = feedback_header_;
    if (profile_frozen_ || (current == ProtocolProfile::V103 && header != missing_header &&
                            (header & v103_drive_enabled) != 0)) return false;
    clear_protocol_feedback();
    protocol_profile_ = requested;
    return true;
}

std::optional<std::string> LroMotorDriver::get_lro_protocol_profile() const {
    return protocol_profile_ == ProtocolProfile::V103 ? "v1.03" : "legacy";
}

std::optional<bool> LroMotorDriver::get_motor_drive_enabled() const {
    std::lock_guard<std::mutex> guard(protocol_mutex_);
    const int header = feedback_header_;
    if (protocol_profile_ != ProtocolProfile::V103 || header == missing_header) return std::nullopt;
    return (header & v103_drive_enabled) != 0;
}

std::optional<uint8_t> LroMotorDriver::get_motor_feedback_header() const {
    const int header = feedback_header_;
    if (header == missing_header) return std::nullopt;
    return static_cast<uint8_t>(header);
}

void LroMotorDriver::clear_protocol_feedback() {
    feedback_header_ = missing_header;
    last_feedback_at_ = {};
    error_id_ = 0;
    motor_pos_ = missing;
    motor_spd_ = missing;
    motor_current_ = missing;
    motor_temperature_ = missing;
    mos_temperature_ = missing;
    iq_current_ = missing;
    dc_bus_voltage_ = missing;
    dc_bus_current_ = missing;
}

void LroMotorDriver::canfd_rx_cbk(const canfd_frame& rx_frame) {
    if (rx_frame.can_id != motor_id_ || rx_frame.len < 2 || rx_frame.len > 64) return;
    if ((rx_frame.data[0] & 0xE0) == 0xA0) {
        std::lock_guard<std::mutex> guard(version_mutex_);
        const uint8_t code = rx_frame.data[1];
        if (pending_version_ == code && std::chrono::steady_clock::now() <= version_deadline_) {
            versions_[code] = std::vector<uint8_t>(rx_frame.data, rx_frame.data + rx_frame.len);
            pending_version_ = 0;
        }
        return; // Query replies are never position/temperature feedback.
    }
    if ((rx_frame.data[0] & 0xE0) != 0x20 || rx_frame.len < 8) return;

    uint8_t error;
    {
        std::lock_guard<std::mutex> guard(protocol_mutex_);
        const auto profile = protocol_profile_.load();
        const bool v103 = profile == ProtocolProfile::V103;
        if (v103 && rx_frame.len != 8 && rx_frame.len != 12) return;
        const auto& ranges = encoding_limits(profile);
        const auto* data = rx_frame.data;
        const uint16_t pos_raw = (static_cast<uint16_t>(data[1]) << 8) | data[2];
        const uint16_t spd_raw = (static_cast<uint16_t>(data[3]) << 4) | (data[4] >> 4);
        const uint16_t feedback_raw = (static_cast<uint16_t>(data[4] & 0x0F) << 8) | data[5];

        error = data[0] & (v103 ? 0x07 : 0x1F);
        error_id_ = error;
        motor_pos_ = range_map(pos_raw, uint16_t(0), bitmax<uint16_t>(16), -ranges.PosMax, ranges.PosMax) +
                     static_cast<float>(motor_zero_offset_);
        motor_spd_ = range_map(spd_raw, uint16_t(0), bitmax<uint16_t>(12), -ranges.SpdMax, ranges.SpdMax);
        if (v103) {
            iq_current_ = range_map(feedback_raw, uint16_t(0), bitmax<uint16_t>(12), -ranges.CurMax, ranges.CurMax);
            if (rx_frame.len == 12) {
                const uint16_t voltage_raw = (static_cast<uint16_t>(data[8]) << 8) | data[9];
                const uint16_t current_raw = (static_cast<uint16_t>(data[10]) << 8) | data[11];
                dc_bus_voltage_ = range_map(voltage_raw, uint16_t(0), bitmax<uint16_t>(16), 0.0f, 100.0f);
                dc_bus_current_ = range_map(current_raw, uint16_t(0), bitmax<uint16_t>(16), -100.0f, 100.0f);
            } else {
                dc_bus_voltage_ = missing;
                dc_bus_current_ = missing;
            }
        } else {
            motor_current_ = range_map(feedback_raw, uint16_t(0), bitmax<uint16_t>(12), -ranges.TauMax, ranges.TauMax);
        }
        motor_temperature_ = static_cast<float>(static_cast<int>(data[6]) - 25);
        mos_temperature_ = static_cast<float>(static_cast<int>(data[7]) - 25);
        feedback_header_ = data[0];
        last_feedback_at_ = std::chrono::steady_clock::now();
        response_count_ = 0;
        feedback_count_++;
    }
    if (error > 0 && logger_) {
        logger_->error("can_interface: {0}\tmotor_id: {1}\terror_id: 0x{2:x}", can_interface_, motor_id_, static_cast<uint32_t>(error));
    }
}

// void LroMotorDriver::ethercat_rx_cbk(const ethercat_frame& rx_frame) {}

void LroMotorDriver::get_motor_param(uint8_t param_cmd) {
    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame{};
        tx_frame.can_id = motor_id_; // ID: Corresponds to the individual motor ID
        tx_frame.len = 0x02; // Query commands typically require only 2 bytes
        tx_frame.flags = CANFD_BRS;

        // Byte 0: Mode bits [Mode(3 bits) | Reserved(5 bits)]
        // Query mode (Mode 0x07) shifted left by 5 bits results in 0xE0
        tx_frame.data[0] = (uint8_t)(LRO_MODE_QUERY << 5);
        tx_frame.data[1] = param_cmd; //// Query (mode 0x07) — request param/status

        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

bool LroMotorDriver::request_version(uint8_t code) {
    if (comm_type_ != CommType::CANFD || (code != 0x31 && code != 0x1E && code != 0x16)) return false;
    {
        std::lock_guard<std::mutex> guard(version_mutex_);
        versions_.erase(code);
        pending_version_ = code;
        version_deadline_ = std::chrono::steady_clock::now() + std::chrono::milliseconds(500);
    }
    canfd_frame query{};
    query.can_id = motor_id_;
    query.len = 2;
    query.flags = CANFD_BRS;
    query.data[0] = 0xE0;
    query.data[1] = code;
    canfd_->transmit(query); // No enable/motion or telemetry-watchdog counter.
    return true;
}

std::vector<uint8_t> LroMotorDriver::version_payload(uint8_t code) {
    std::lock_guard<std::mutex> guard(version_mutex_);
    auto it = versions_.find(code);
    return it == versions_.end() ? std::vector<uint8_t>{} : it->second;
}

void LroMotorDriver::motor_pos_cmd(float pos, float spd, bool ignore_limit) {
    if (motor_control_mode_ != POS) {
        set_motor_control_mode(POS);
        return;
    }
    float pos_deg = (pos - static_cast<float>(motor_zero_offset_)) * 180.0f / static_cast<float>(M_PI);
    float spd_rpm = std::abs(spd) * 60.0f / (2.0f * static_cast<float>(M_PI));
    uint16_t spd_val = static_cast<uint16_t>(limit(spd_rpm * 10.0f, 0.0f, 32767.0f));
    const auto profile = protocol_profile_.load();
    const uint16_t cur_limit = profile == ProtocolProfile::V103
        ? static_cast<uint16_t>(limit(encoding_limits(profile).CurMax * 10.0f, 0.0f, 4095.0f)) : 4095;
    uint8_t ack = 1;
    union32_t rv_type_convert;
    rv_type_convert.f = pos_deg;

    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame;
        tx_frame.can_id = motor_id_;
        tx_frame.len = 0x08;
        tx_frame.flags = CANFD_BRS;

        uint64_t packed = 0;
        packed |= (static_cast<uint64_t>(LRO_MODE_POS & 0x07)) << 61;
        packed |= (static_cast<uint64_t>(rv_type_convert.buf[3]) << 53) | (static_cast<uint64_t>(rv_type_convert.buf[2]) << 45)
                 | (static_cast<uint64_t>(rv_type_convert.buf[1]) << 37) | (static_cast<uint64_t>(rv_type_convert.buf[0]) << 29);
        packed |= (static_cast<uint64_t>(spd_val & 0x7FFF)) << 14;
        packed |= (static_cast<uint64_t>(cur_limit & 0x0FFF)) << 2;
        packed |= (static_cast<uint64_t>(ack & 0x03));

        for (int i = 0; i < 8; ++i) {
            tx_frame.data[i] = (packed >> (56 - i * 8)) & 0xFF;
        }

        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

void LroMotorDriver::motor_spd_cmd(float spd) {
    if (motor_control_mode_ != SPD) {
        set_motor_control_mode(SPD);
        return;
    }
    float spd_rpm = spd * 60.0f / (2.0f * static_cast<float>(M_PI));
    const auto profile = protocol_profile_.load();
    const uint16_t cur_limit = profile == ProtocolProfile::V103
        ? static_cast<uint16_t>(limit(encoding_limits(profile).CurMax * 10.0f, 0.0f, 65535.0f)) : 65535;
    uint8_t ack = 1;
    union32_t rv_type_convert;
    rv_type_convert.f = spd_rpm;

    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame;
        tx_frame.can_id = motor_id_;
        tx_frame.len = 0x07;
        tx_frame.flags = CANFD_BRS;

        tx_frame.data[0] = ((LRO_MODE_SPD & 0x07) << 5) | (ack & 0x03);
        tx_frame.data[1] = rv_type_convert.buf[3];
        tx_frame.data[2] = rv_type_convert.buf[2];
        tx_frame.data[3] = rv_type_convert.buf[1];
        tx_frame.data[4] = rv_type_convert.buf[0];
        tx_frame.data[5] = (cur_limit >> 8) & 0xFF;
        tx_frame.data[6] = cur_limit & 0xFF;

        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

// Transmit MIT-mDme control(hybrid) package. Called in canTask.
// ------------------------------------------------------------------
// MIT impedance control (mode 0x00)
// Protocol bit layout (8 bytes, standard CAN, ID = motor_id:
//   motor_mode  uint3   (bits 63-61)
//   KP          uint12  (bits 60-49)
//   KD          uint9   (bits 48-40)
//   pos         uint16  (bits 39-24)
//   spd         uint12  (bits 23-12)
//   torque      uint12  (bits 11-0)
// ------------------------------------------------------------------
void LroMotorDriver::pack_mit(float pos, float spd, float kp, float kd, float torque, uint8_t* data) const {
    const auto& ranges = encoding_limits(protocol_profile_.load());
    const auto encode = [](float value, float low, float high, uint16_t max_raw) {
        return range_map(limit(value, low, high), low, high, uint16_t(0), max_raw);
    };
    const auto p = encode(pos - static_cast<float>(motor_zero_offset_), -ranges.PosMax, ranges.PosMax, 0xFFFF);
    const auto v = encode(spd, -ranges.SpdMax, ranges.SpdMax, 0x0FFF);
    const auto p_gain = encode(kp, 0.0f, ranges.OKpMax, 0x0FFF);
    const auto d_gain = encode(kd, 0.0f, ranges.OKdMax, 0x01FF);
    const auto t = encode(torque, -ranges.TauMax, ranges.TauMax, 0x0FFF);
    const uint64_t packed = (uint64_t(LRO_MODE_MIT) << 61) | (uint64_t(p_gain) << 49) |
                            (uint64_t(d_gain) << 40) | (uint64_t(p) << 24) | (uint64_t(v) << 12) | t;
    for (size_t i = 0; i < 8; ++i) {
        data[i] = static_cast<uint8_t>(packed >> (56 - i * 8));
    }
}

void LroMotorDriver::motor_mit_cmd(float f_p, float f_v, float f_kp, float f_kd, float f_t) {
    if (motor_control_mode_ != MIT) {
        set_motor_control_mode(MIT);
        return;
    }
    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame{};
        tx_frame.can_id = motor_id_;
        tx_frame.len = 0x08;
        tx_frame.flags = CANFD_BRS;
        pack_mit(f_p, f_v, f_kp, f_kd, f_t, tx_frame.data);
        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

void LroMotorDriver::motor_mit_cmd(float* f_p, float* f_v, float* f_kp, float* f_kd, float* f_t) {
    if (!f_p || !f_v || !f_kp || !f_kd || !f_t) {
        return;
    }
    if (motor_control_mode_ != MIT) {
        set_motor_control_mode(MIT);
        return;
    }
    canfd_frame tx_frame{};
    tx_frame.can_id = 0x8080 | CAN_EFF_FLAG;
    tx_frame.len = 64;
    tx_frame.flags = CANFD_BRS;

    std::shared_lock<std::shared_mutex> lock(bus_registry_mutex_);
    auto it = bus_registry_.find(can_interface_);
    if (it != bus_registry_.end()) {
        for (LroMotorDriver* motor : it->second) {
            if (motor->canfd_ != canfd_ || motor->motor_index_ >= 8) {
                continue;
            }
            const uint8_t slot = motor->motor_index_;
            motor->pack_mit(f_p[slot], f_v[slot], f_kp[slot], f_kd[slot], f_t[slot], &tx_frame.data[slot * 8]);
        }
        for (LroMotorDriver* motor : it->second) {
            if (motor->canfd_ == canfd_ && motor->motor_index_ < 8) {
                motor->response_count_++;
            }
        }
    }

    canfd_->transmit(tx_frame);
}

void LroMotorDriver::set_motor_control_mode(uint8_t motor_control_mode) {
    if (motor_control_mode_ == motor_control_mode) {
        return;
    }
    motor_control_mode_ = motor_control_mode;
}

void LroMotorDriver::set_motor_id(uint8_t old_id, uint8_t new_id) {
    // Parameter Validation: LRO supports IDs from 1 to 0x7FF
    if (old_id < 1 || old_id > 0x7FF || new_id < 1 || new_id > 0x7FF) {
        logger_->error("Invalid ID range: old={}, new={}", old_id, new_id);
        return;
    }
    if (old_id == new_id) {
        return;
    }
    logger_->info("Changing Motor ID: {} -> {} (Interface: {})", old_id, new_id, can_interface_);
    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame{};
        tx_frame.can_id = 0x7FF; // Dedicated Configuration ID
        tx_frame.len = 0x06;
        tx_frame.flags = CANFD_BRS;

        // Populate Protocol Message
        tx_frame.data[0] = (old_id >> 8) & 0xFF;   // Old ID High Byte
        tx_frame.data[1] = old_id & 0xFF;          // Old ID Low Byte
        tx_frame.data[2] = 0x00;                   // Direction: Master -> Slave
        tx_frame.data[3] = LRO_CMD_SET_ID;         // Command: 0x04
        tx_frame.data[4] = (new_id >> 8) & 0xFF;   // New ID High Byte
        tx_frame.data[5] = new_id & 0xFF;          // New ID Low Byte

        canfd_->transmit(tx_frame);
        {
            response_count_++;
        }

        // Allow buffer time for Flash write persistence
        Timer::sleep_for(setup_sleep_time);
    }
}

void LroMotorDriver::reset_motor_id() {
    if (comm_type_ == CommType::CANFD) {
        // Reset all motors on the bus to ID 0x01
        canfd_frame tx_frame{};
        tx_frame.can_id = 0x7FF;
        tx_frame.len = 0x06;
        tx_frame.flags = CANFD_BRS;

        tx_frame.data[0] = 0x7F;
        tx_frame.data[1] = 0x7F;
        tx_frame.data[2] = 0x00;
        tx_frame.data[3] = LRO_CMD_RESET_ID;
        tx_frame.data[4] = 0x7F;
        tx_frame.data[5] = 0x7F;

        canfd_->transmit(tx_frame);
    }
    {
        response_count_++;
    }
}

canfd_frame LroMotorDriver::zero_request_frame() const {
    canfd_frame frame{};
    frame.can_id = 0x7FF;
    frame.len = 0x04;
    frame.flags = CANFD_BRS;
    frame.data[0] = (motor_id_ >> 8) & 0xFF;
    frame.data[1] = motor_id_ & 0xFF;
    frame.data[3] = LRO_CMD_SET_ZERO;
    return frame;
}


void LroMotorDriver::clear_motor_error_lro() {
    if (comm_type_ == CommType::CANFD) {
        canfd_frame tx_frame{};
        tx_frame.can_id = 0x7FF;
        tx_frame.len = 0x04;
        tx_frame.flags = CANFD_BRS;

        tx_frame.data[0] = (motor_id_ >> 8) & 0xFF;
        tx_frame.data[1] = motor_id_ & 0xFF;
        tx_frame.data[2] = 0x00;
        tx_frame.data[3] = LRO_CMD_CLEAR_ERROR;

        canfd_->transmit(tx_frame);
        response_count_++;
    }
}

void LroMotorDriver::write_register_lro(uint8_t rid, float value) {
    return;
}

void LroMotorDriver::write_register_lro(uint8_t index, int32_t value) {
    return;
}

void LroMotorDriver::save_register_lro() {
    // LRO protocol has no dedicated "save to flash" command.
    return;
}

void LroMotorDriver::refresh_motor_status() {
    // Send a zero MIT command with type-1 feedback to get current status
    motor_mit_cmd(0.0f, 0.0f, 0.0f, 0.0f, 0.0f);
}

void LroMotorDriver::clear_motor_error() {
    clear_motor_error_lro();
}
