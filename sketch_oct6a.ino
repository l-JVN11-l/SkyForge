
#include <Stepper.h>
#include <Servo.h>
#include <string.h>
#include <stdio.h>

// ==================== STEPPER MOTOR ====================

const int STEPS_PER_REVOLUTION = 2048;

const int IN1 = 4;
const int IN2 = 5;
const int IN3 = 6;
const int IN4 = 7;

Stepper motor(
  STEPS_PER_REVOLUTION,
  IN1, IN3, IN2, IN4
);

const long STEPPER_MAX_STEPS = STEPS_PER_REVOLUTION;

const int STEPPER_MIN_RPM = 1;
const int STEPPER_MAX_RPM = 12;
const int STEPPER_RESET_RPM = 8;

long stepperPosition = 0;
int stepperInput = 0;

// ==================== SERVO MOTOR ====================

Servo servo;

int servoAngle = 90;

const int SERVO_MIN_ANGLE = 15;
const int SERVO_MAX_ANGLE = 165;
const int SERVO_PIN = 9;

const int SERVO_SLOW_INTERVAL = 60;
const int SERVO_FAST_INTERVAL = 8;

int servoInput = 0;
unsigned long lastServoMoveTime = 0;

// ==================== TURRET RESET ====================

bool resetInProgress = false;

// ==================== SOLENOID ====================

const int SOLENOID_PIN = 8;

bool launching = false;
unsigned long launchStart = 0;
const unsigned long LAUNCH_DURATION = 2000;

// ==================== SERIAL INPUT ====================

char inputBuffer[40];
byte inputIndex = 0;

// ==================== PROCESS COMMAND ====================

void processCommand(char *line) {
  // Xbox X: activate solenoid
  if (strcmp(line, "X") == 0) {
    if (!launching) {
      digitalWrite(SOLENOID_PIN, HIGH);
      launchStart = millis();
      launching = true;
    }
    return;
  }

  // Xbox B: reset turret position
  if (strcmp(line, "B") == 0) {
    // Stop joystick-controlled movement.
    servoInput = 0;
    stepperInput = 0;

    // Command the servo back to straight up.
    servoAngle = 90;
    servo.write(servoAngle);

    // Return the stepper to tracked horizontal zero.
    resetInProgress = true;

    Serial.println("Turret reset started.");
    return;
  }

  // Ignore joystick commands during horizontal reset.
  if (resetInProgress) {
    return;
  }

  // Joystick command: J,servoValue,stepperValue
  if (strncmp(line, "J,", 2) == 0) {
    int newServoInput;
    int newStepperInput;

    if (sscanf(line + 2, "%d,%d",
               &newServoInput,
               &newStepperInput) == 2) {

      servoInput = constrain(newServoInput, -1000, 1000);
      stepperInput = constrain(newStepperInput, -1000, 1000);
    }
  }
}

// ==================== SETUP ====================

void setup() {
  Serial.begin(9600);

  motor.setSpeed(STEPPER_MIN_RPM);

  servo.attach(SERVO_PIN);
  servo.write(servoAngle);

  pinMode(SOLENOID_PIN, OUTPUT);
  digitalWrite(SOLENOID_PIN, LOW);

  Serial.println("Proportional turret controller ready.");
  Serial.println("J,servo,stepper: joystick control");
  Serial.println("X: activate solenoid");
  Serial.println("B: reset to horizontal 0, vertical 90");
  Serial.println("Stepper range: 0-360 degrees");
  Serial.println("Servo range: 15-165 degrees");
}

// ==================== MAIN LOOP ====================

void loop() {
  unsigned long now = millis();

  // Read newline-terminated serial messages.
  while (Serial.available() > 0) {
    char incoming = Serial.read();

    if (incoming == '\r') {
      continue;
    }

    if (incoming == '\n') {
      inputBuffer[inputIndex] = '\0';

      if (inputIndex > 0) {
        processCommand(inputBuffer);
      }

      inputIndex = 0;
    } else {
      if (inputIndex < sizeof(inputBuffer) - 1) {
        inputBuffer[inputIndex++] = incoming;
      } else {
        inputIndex = 0;
      }
    }
  }

  // ==================== HORIZONTAL RESET ====================

  if (resetInProgress) {
    // Move toward zero at a fixed, moderate speed.
    if (stepperPosition > 0) {
      motor.setSpeed(STEPPER_RESET_RPM);
      motor.step(-1);
      stepperPosition--;
    }

    if (stepperPosition <= 0) {
      stepperPosition = 0;
      stepperInput = 0;
      servoInput = 0;
      resetInProgress = false;

      Serial.println("Turret reset complete.");
    }
  }

  // ==================== NORMAL STEPPER CONTROL ====================

  else if (stepperInput != 0) {
    int magnitude = abs(stepperInput);

    int rpm = map(
      magnitude, 1, 1000,
      STEPPER_MIN_RPM, STEPPER_MAX_RPM
    );

    rpm = constrain(rpm, STEPPER_MIN_RPM, STEPPER_MAX_RPM);
    motor.setSpeed(rpm);

    // Positive = R; negative = L.
    if (stepperInput > 0 &&
        stepperPosition < STEPPER_MAX_STEPS) {
      motor.step(1);
      stepperPosition++;
    }
    else if (stepperInput < 0 &&
             stepperPosition > 0) {
      motor.step(-1);
      stepperPosition--;
    }
  }

  // ==================== SERVO CONTROL ====================

  if (!resetInProgress && servoInput != 0) {
    int magnitude = abs(servoInput);

    int moveInterval = map(
      magnitude, 1, 1000,
      SERVO_SLOW_INTERVAL, SERVO_FAST_INTERVAL
    );

    moveInterval = constrain(
      moveInterval,
      SERVO_FAST_INTERVAL,
      SERVO_SLOW_INTERVAL
    );

    if (now - lastServoMoveTime >=
        (unsigned long)moveInterval) {

      lastServoMoveTime = now;

      // Positive = UP; negative = DOWN.
      if (servoInput > 0 &&
          servoAngle < SERVO_MAX_ANGLE) {
        servoAngle++;
        servo.write(servoAngle);
      }
      else if (servoInput < 0 &&
               servoAngle > SERVO_MIN_ANGLE) {
        servoAngle--;
        servo.write(servoAngle);
      }
    }
  }

  // ==================== SOLENOID TIMER ====================

  if (launching &&
      now - launchStart >= LAUNCH_DURATION) {
    digitalWrite(SOLENOID_PIN, LOW);
    launching = false;
  }

  // ==================== TELEMETRY ====================

  static unsigned long lastTelemetryTime = 0;

  if (millis() - lastTelemetryTime >= 100) {
    lastTelemetryTime = millis();

    long stepperAngle =
      (stepperPosition * 360L) / STEPPER_MAX_STEPS;

    Serial.print("POS,");
    Serial.print(stepperAngle);
    Serial.print(",");
    Serial.print(servoAngle);
    Serial.print(",");
    Serial.println(launching ? 1 : 0);
  }
}