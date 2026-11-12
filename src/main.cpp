#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>
#include <errno.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>

constexpr uint8_t SERVO_COUNT = 24;
constexpr uint8_t SERVOS_PER_BOARD = 12;
constexpr uint8_t BOARD_COUNT = 2;
constexpr uint8_t PCA_ADDRESS[BOARD_COUNT] = {0x40, 0x60};
constexpr int SDA_PIN = D4; // XIAO ESP32S3: GPIO5
constexpr int SCL_PIN = D5; // XIAO ESP32S3: GPIO6
constexpr float SERVO_FREQUENCY = 50.0f;
constexpr uint16_t CENTER_US = 1500;
// 默认脉宽限位，可在 setup() 中逐路标定。
constexpr uint16_t DEFAULT_MIN_US = 1000;
constexpr uint16_t DEFAULT_MAX_US = 2300;
constexpr uint32_t STEP_INTERVAL_MS = 20;
constexpr uint32_t MAX_DURATION_MS = 60000;
constexpr uint32_t BOARD_CHECK_INTERVAL_MS = 3000;
constexpr long MAX_JOG_US = 100;

struct ServoState {
    uint16_t minUs = DEFAULT_MIN_US;
    uint16_t maxUs = DEFAULT_MAX_US;
    uint16_t currentUs = CENTER_US; // 最后一次成功发送值，不是实际位置反馈
    uint16_t startUs = CENTER_US;
    uint16_t targetUs = CENTER_US;
    uint32_t startMs = 0;
    uint32_t durationMs = 0;
    bool enabled = false;
    bool moving = false;
};

Adafruit_PWMServoDriver pca1(PCA_ADDRESS[0]);
Adafruit_PWMServoDriver pca2(PCA_ADDRESS[1]);
Adafruit_PWMServoDriver *const boards[BOARD_COUNT] = {&pca1, &pca2};
ServoState servos[SERVO_COUNT];
bool boardReady[BOARD_COUNT] = {};
float ticksPerUs[BOARD_COUNT] = {};
bool testActive = false;
int testSelection = -1; // -1: 全部；其余为舵机编号
uint8_t testPhase = 0;
uint32_t testPhaseMs = 0;
bool testHolding = false;
bool demoActive = false;
float demoAngle = 135.0f;

void printHelp()
{
    Serial.println("servo N US | angle N DEG | smooth N US MS");
    Serial.println("jog N DELTA_US (-100..100)");
    Serial.println("test N | demo [DEG] | center | minall | maxall | testall | stop | help");
    Serial.println("N=0..23, DEG=0..180 (mapped range, not measured degrees)");
    Serial.printf("Default pulse range: %u..%u us; smooth: 0..%lu ms\n",
                  DEFAULT_MIN_US, DEFAULT_MAX_US,
                  static_cast<unsigned long>(MAX_DURATION_MS));
}

bool parseInteger(const char *text, long minimum, long maximum, long &value)
{
    char *end = nullptr;
    errno = 0;
    value = strtol(text, &end, 10);
    return end != text && *end == '\0' && errno != ERANGE &&
           value >= minimum && value <= maximum;
}

bool parseAngle(const char *text, float &angle)
{
    char *end = nullptr;
    errno = 0;
    angle = strtof(text, &end);
    return end != text && *end == '\0' && errno != ERANGE &&
           isfinite(angle) && angle >= 0.0f && angle <= 180.0f;
}

bool writeServoPulse(uint8_t index, uint16_t pulseUs)
{
    ServoState &servo = servos[index];
    const uint8_t board = index / SERVOS_PER_BOARD;
    if (!boardReady[board]) {
        Serial.printf("ERROR PCA9685 0x%02X unavailable\n", PCA_ADDRESS[board]);
        return false;
    }
    if (pulseUs < servo.minUs || pulseUs > servo.maxUs) {
        Serial.printf("ERROR servo %u range: %u..%u us\n", index, servo.minUs, servo.maxUs);
        return false;
    }
    // 按芯片实际预分频换算，缓存比例，避免每次写入前再读 I2C。
    const uint16_t ticks = static_cast<uint16_t>(pulseUs * ticksPerUs[board] + 0.5f);
    if (boards[board]->setPWM(index % SERVOS_PER_BOARD, 0, ticks) != 0) {
        Serial.printf("ERROR I2C write, servo %u\n", index);
        return false;
    }
    servo.currentUs = pulseUs;
    servo.enabled = true;
    return true;
}

bool startMove(uint8_t index, uint16_t targetUs, uint32_t durationMs)
{
    ServoState &servo = servos[index];
    servo.moving = false;
    if (durationMs == 0) {
        return writeServoPulse(index, targetUs);
    }
    if (!servo.enabled) {
        Serial.printf("ERROR send 'servo %u 1500' first; actual position is unknown\n", index);
        return false;
    }
    servo.startUs = servo.currentUs;
    servo.targetUs = targetUs;
    servo.startMs = millis();
    servo.durationMs = durationMs;
    servo.moving = true;
    return true;
}

void updateMoves()
{
    static uint32_t lastUpdateMs = 0;
    const uint32_t now = millis();
    if (static_cast<uint32_t>(now - lastUpdateMs) < STEP_INTERVAL_MS) {
        return;
    }
    lastUpdateMs = now;
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        ServoState &servo = servos[i];
        if (!servo.moving) {
            continue;
        }
        const uint32_t elapsed = now - servo.startMs; // 支持 millis() 回绕
        const bool finished = elapsed >= servo.durationMs;
        const float progress = finished ? 1.0f : static_cast<float>(elapsed) / servo.durationMs;
        const int32_t delta = static_cast<int32_t>(servo.targetUs) - servo.startUs;
        const uint16_t pulse = static_cast<uint16_t>(servo.startUs + delta * progress + 0.5f);
        if ((pulse != servo.currentUs || finished) && !writeServoPulse(i, pulse)) {
            servo.moving = false;
            testActive = false;
        } else if (finished) {
            servo.moving = false;
        }
    }
}

// mode: -1=每路下限，0=1500us（夹到各自范围），1=每路上限。
bool setAllServos(int mode)
{
    bool ok = true;
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        const ServoState &servo = servos[i];
        const uint16_t pulse = mode < 0 ? servo.minUs : mode > 0 ? servo.maxUs :
            static_cast<uint16_t>(constrain(CENTER_US, servo.minUs, servo.maxUs));
        if (!startMove(i, pulse, 0)) {
            ok = false;
        }
    }
    return ok;
}

void stopAllServos()
{
    testActive = false;
    bool ok = true;
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        ServoState &servo = servos[i];
        servo.moving = false;
        const uint8_t board = i / SERVOS_PER_BOARD;
        if (boardReady[board] &&
            boards[board]->setPWM(i % SERVOS_PER_BOARD, 0, 4096) == 0) {
            servo.enabled = false;
        } else {
            ok = false;
        }
    }
    Serial.println(ok ? "OK PWM off; holding torque is not guaranteed" :
                        "ERROR stop incomplete; check power/wiring");
}

bool testIncludesServo(uint8_t index)
{
    return demoActive ? index < 3 : testSelection < 0 || index == testSelection;
}

bool applyTestPhase()
{
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        if (!testIncludesServo(i)) {
            continue;
        }
        const ServoState &servo = servos[i];
        uint16_t pulse = testPhase == 0 ? servo.minUs : testPhase == 1 ? servo.maxUs :
            static_cast<uint16_t>(constrain(CENTER_US, servo.minUs, servo.maxUs));
        if (demoActive) {
            pulse = testPhase == 1 ? static_cast<uint16_t>(lroundf(
                servo.minUs + (servo.maxUs - servo.minUs) * demoAngle / 180.0f)) : CENTER_US;
        }
        // 样本第一阶段先给三路默认位置；首次发送无法保证实际平滑。
        const uint32_t duration = demoActive && testPhase == 0 && !servo.enabled ? 0 : 500;
        if (!startMove(i, pulse, duration)) {
            return false;
        }
    }
    testHolding = false;
    return true;
}

// 样本：仅操作 0/1/2，默认位置 -> 同时移动 -> 保持 2 秒 -> 返回默认位置。
// angle 沿用 angle 命令的范围映射，默认 135 对应 1975us。
void demoServo012(float angle = 135.0f)
{
    if (!isfinite(angle) || angle < 0.0f || angle > 180.0f || !boardReady[0]) {
        Serial.println("ERROR demo requires PCA9685 0x40 and angle 0..180");
        return;
    }
    for (uint8_t i = 0; i < 3; ++i) {
        if (CENTER_US < servos[i].minUs || CENTER_US > servos[i].maxUs) {
            Serial.println("ERROR demo requires 1500us within servo 0/1/2 limits");
            return;
        }
    }
    demoActive = true;
    demoAngle = angle;
    testPhase = 0;
    testActive = applyTestPhase();
    if (testActive) {
        Serial.printf("OK demo 0/1/2: 1500us -> mapped %.1f -> hold 2s -> 1500us\n", angle);
    }
}

void startTest(int selection)
{
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        if ((selection < 0 || selection == i) && !servos[i].enabled) {
            Serial.println("ERROR initialize selected servo(s) first; use servo N 1500 / center");
            return;
        }
    }
    demoActive = false;
    testSelection = selection;
    testPhase = 0;
    testActive = applyTestPhase();
    if (testActive) {
        Serial.println("OK test started (min -> max -> 1500 us)");
    }
}

void updateTest()
{
    if (!testActive) {
        return;
    }
    // 等三路的目标指令全部发送完成，再计时保持；这不是位置反馈。
    for (uint8_t i = 0; i < SERVO_COUNT; ++i) {
        if (testIncludesServo(i) && servos[i].moving) {
            return;
        }
    }
    if (!testHolding) {
        testHolding = true;
        testPhaseMs = millis();
    }
    const uint32_t holdMs = demoActive ? (testPhase == 0 ? 500 : testPhase == 1 ? 2000 : 0) : 300;
    if (static_cast<uint32_t>(millis() - testPhaseMs) < holdMs) {
        return;
    }
    if (++testPhase >= 3) {
        testActive = false;
        Serial.println(demoActive ? "OK demo complete, servo 0/1/2 at 1500us command" : "OK test complete");
    } else {
        testActive = applyTestPhase();
    }
}

void processCommand(char *line)
{
    char *args[4];
    uint8_t count = 0;
    char *context = nullptr;
    for (char *token = strtok_r(line, " \t\r", &context); token;
         token = strtok_r(nullptr, " \t\r", &context)) {
        if (count == 4) {
            Serial.println("ERROR too many arguments");
            return;
        }
        args[count++] = token;
    }
    if (count == 0) {
        return;
    }
    if (count == 1) {
        if (strcmp(args[0], "help") == 0) {
            printHelp();
        } else if (strcmp(args[0], "stop") == 0) {
            stopAllServos();
        } else if (strcmp(args[0], "testall") == 0) {
            startTest(-1);
        } else if (strcmp(args[0], "demo") == 0) {
            demoServo012();
        } else if (strcmp(args[0], "center") == 0 || strcmp(args[0], "minall") == 0 ||
                   strcmp(args[0], "maxall") == 0) {
            testActive = false;
            const int mode = strcmp(args[0], "minall") == 0 ? -1 :
                             strcmp(args[0], "maxall") == 0 ? 1 : 0;
            if (setAllServos(mode)) {
                Serial.println("OK all pulses written (not position feedback)");
            }
        } else {
            Serial.println("ERROR unknown command; type help");
        }
        return;
    }
    if (strcmp(args[0], "demo") == 0 && count == 2) {
        float angle = 0.0f;
        if (parseAngle(args[1], angle)) {
            demoServo012(angle);
        } else {
            Serial.println("ERROR angle must be finite and within 0..180");
        }
        return;
    }
    const bool direct = strcmp(args[0], "servo") == 0 && count == 3;
    const bool angleCommand = strcmp(args[0], "angle") == 0 && count == 3;
    const bool smooth = strcmp(args[0], "smooth") == 0 && count == 4;
    const bool test = strcmp(args[0], "test") == 0 && count == 2;
    const bool jog = strcmp(args[0], "jog") == 0 && count == 3;
    if (!direct && !angleCommand && !smooth && !test && !jog) {
        Serial.println("ERROR command/argument count; type help");
        return;
    }
    long indexValue = 0;
    if (!parseInteger(args[1], 0, SERVO_COUNT - 1, indexValue)) {
        Serial.println("ERROR servo index must be 0..23");
        return;
    }
    const uint8_t index = static_cast<uint8_t>(indexValue); // 校验后再转换
    if (test) {
        startTest(index);
        return;
    }
    const ServoState &servo = servos[index];
    if (jog) {
        long delta = 0;
        if (!parseInteger(args[2], -MAX_JOG_US, MAX_JOG_US, delta) || delta == 0) {
            Serial.println("ERROR jog step must be 1..100 us in either direction");
        } else if (!servo.enabled) {
            Serial.printf("ERROR servo %u needs an initial pulse\n", index);
        } else if (servo.moving || (testActive && testIncludesServo(index))) {
            Serial.printf("ERROR servo %u is moving\n", index);
        } else {
            const long requested = static_cast<long>(servo.currentUs) + delta;
            const uint16_t pulse = static_cast<uint16_t>(constrain(requested, servo.minUs, servo.maxUs));
            if (writeServoPulse(index, pulse)) {
                Serial.printf("OK jog %u pulse %u us\n", index, pulse);
            }
        }
        return;
    }
    long pulse = 0;
    long duration = 0;
    if (angleCommand) {
        float angle = 0.0f;
        if (!parseAngle(args[2], angle)) {
            Serial.println("ERROR angle must be finite and within 0..180");
            return;
        }
        pulse = lroundf(servo.minUs + (servo.maxUs - servo.minUs) * angle / 180.0f);
    } else if (!parseInteger(args[2], servo.minUs, servo.maxUs, pulse)) {
        Serial.printf("ERROR pulse must be %u..%u us\n", servo.minUs, servo.maxUs);
        return;
    }
    if (smooth && !parseInteger(args[3], 0, MAX_DURATION_MS, duration)) {
        Serial.println("ERROR duration must be 0..60000 ms");
        return;
    }
    testActive = false; // 新控制命令取消自动测试的后续步骤
    if (startMove(index, static_cast<uint16_t>(pulse), static_cast<uint32_t>(duration))) {
        Serial.printf("OK servo %u target %ld us, duration %ld ms\n", index, pulse, duration);
    }
}

void pollSerial()
{
    static char line[96];
    static size_t length = 0;
    static bool discard = false;
    // 每次处理有限字节，避免连续输入饿死运动更新。
    for (uint8_t budget = 0; budget < 64 && Serial.available(); ++budget) {
        const char ch = static_cast<char>(Serial.read());
        if (ch == '\n') {
            if (!discard) {
                line[length] = '\0';
                processCommand(line);
            }
            length = 0;
            discard = false;
        } else if (!discard) {
            if (ch == '\0' || length >= sizeof(line) - 1) {
                discard = true; // 整行丢弃，不能把后半截误当新指令
                Serial.println("ERROR invalid/too long line; discarded until newline");
            } else {
                line[length++] = ch;
            }
        }
    }
}

void checkAndInitializeServoBoards()
{
    uint8_t connectedCount = 0;
    uint8_t readyCount = 0;
    Serial.println("Checking PCA9685 boards...");
    for (uint8_t board = 0; board < BOARD_COUNT; ++board) {
        boardReady[board] = false;
        ticksPerUs[board] = 0.0f;
        Wire.beginTransmission(PCA_ADDRESS[board]);
        const uint8_t error = Wire.endTransmission();
        if (error != 0) {
            Serial.printf("Board %u (0x%02X): not responding (I2C error %u)\n",
                          board + 1, PCA_ADDRESS[board], error);
            continue;
        }
        ++connectedCount;
        if (!boards[board]->begin()) {
            Serial.printf("Board %u (0x%02X): detected, initialization failed\n",
                          board + 1, PCA_ADDRESS[board]);
            continue;
        }
        bool ok = true;
        for (uint8_t channel = 0; channel < 16; ++channel) {
            if (boards[board]->setPWM(channel, 0, 4096) != 0) {
                ok = false;
            }
        }
        boards[board]->setPWMFreq(SERVO_FREQUENCY);
        const uint8_t prescale = boards[board]->readPrescale();
        ticksPerUs[board] = static_cast<float>(boards[board]->getOscillatorFrequency()) /
                           ((static_cast<uint16_t>(prescale) + 1) * 1000000.0f);
        boardReady[board] = ok;
        if (ok) {
            ++readyCount;
        }
        Serial.printf("Board %u (0x%02X): %s\n", board + 1, PCA_ADDRESS[board],
                      ok ? "connected, ready, PWM off" : "detected, PWM initialization failed");
    }
    Serial.printf("Servo boards: %u/%u responding, %u/%u ready\n",
                  connectedCount, BOARD_COUNT, readyCount, BOARD_COUNT);
    if (readyCount == BOARD_COUNT) {
        Serial.println("OK both servo driver boards are ready");
    } else {
        Serial.println("WARNING check VCC/GND/SDA/SCL and address jumpers");
        Serial.println("Only ready boards can be controlled");
    }
}

// 运行时只检查地址响应，不重新初始化或改写 PWM。
void checkServoBoardConnections()
{
    uint8_t connectedCount = 0;
    for (uint8_t board = 0; board < BOARD_COUNT; ++board) {
        Wire.beginTransmission(PCA_ADDRESS[board]);
        const uint8_t error = Wire.endTransmission();
        if (error == 0) {
            ++connectedCount;
            Serial.printf("Board %u (0x%02X): connected\n", board + 1, PCA_ADDRESS[board]);
        } else {
            Serial.printf("Board %u (0x%02X): not responding (I2C error %u)\n",
                          board + 1, PCA_ADDRESS[board], error);
        }
    }
    Serial.printf("I2C check: %u/%u boards responding\n", connectedCount, BOARD_COUNT);
}

void setup()
{
    Serial.begin(115200);
    delay(1000); // 仅启动时等待 USB 串口；运动和命令处理不使用 delay
    Wire.begin(SDA_PIN, SCL_PIN);
    Wire.setClock(400000);
    Wire.setTimeOut(20);
    // 如需逐路标定，可在这里分别设置 servos[i].minUs / maxUs。
    checkAndInitializeServoBoards();
    printHelp();
}

void loop()
{
    static uint32_t lastCheckMs = millis();
    pollSerial();
    updateMoves();
    updateTest();
    const uint32_t now = millis();
    if (static_cast<uint32_t>(now - lastCheckMs) >= BOARD_CHECK_INTERVAL_MS) {
        lastCheckMs = now;
        checkServoBoardConnections();
    }
    yield();
}
