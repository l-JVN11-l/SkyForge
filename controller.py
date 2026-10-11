
from inputs import get_gamepad
import serial
import time
import threading
import queue

# ==================== SERIAL ====================

arduino = serial.Serial("COM3", 9600, timeout=0)
time.sleep(2)

serial_lock = threading.Lock()
events_queue = queue.Queue()

# ==================== CONTROLLER ====================

left_joystick_y = 0
right_joystick_x = 0

DEADZONE = 8000
AXIS_MAX = 32767
SEND_INTERVAL = 0.05  # 50 ms

state_lock = threading.Lock()


def normalize_axis(value):
    """Convert raw joystick input to a value from -1000 to 1000."""
    value = max(-AXIS_MAX, min(AXIS_MAX, value))
    magnitude = abs(value)

    if magnitude <= DEADZONE:
        return 0

    scaled = (magnitude - DEADZONE) / (AXIS_MAX - DEADZONE)
    scaled = max(0.0, min(1.0, scaled))

    result = round(scaled * 1000)
    return result if value >= 0 else -result


def read_controller():
    global left_joystick_y, right_joystick_x

    while True:
        try:
            events = get_gamepad()

            for event in events:
                if event.ev_type == "Absolute":
                    with state_lock:
                        if event.code == "ABS_Y":
                            left_joystick_y = event.state

                        elif event.code == "ABS_RX":
                            right_joystick_x = event.state

                elif event.ev_type == "Key" and event.state == 1:
                    # Xbox X button
                    if event.code == "BTN_WEST":
                        events_queue.put(b"X\n")

                    # Xbox B button
                    elif event.code == "BTN_EAST":
                        # Clear the last joystick values so the
                        # turret doesn't immediately resume movement.
                        with state_lock:
                            left_joystick_y = 0
                            right_joystick_x = 0

                        events_queue.put(b"B\n")

        except Exception as error:
            print(f"Controller read error: {error}")
            time.sleep(0.1)


def send_command(command):
    with serial_lock:
        if arduino.is_open:
            arduino.write(command)


thread = threading.Thread(target=read_controller, daemon=True)
thread.start()

print("Xbox controller ready!")
print("Left joystick UP/DOWN: servo vertical movement")
print("Right joystick LEFT/RIGHT: stepper horizontal movement")
print("Small movement = slow; full movement = fast")
print("Xbox X = activate solenoid")
print("Xbox B = reset turret to horizontal 0°, vertical 90°")
print("Press Ctrl+C to exit")

try:
    while True:
        with state_lock:
            left_y = left_joystick_y
            right_x = right_joystick_x

        # Left joystick: negative raw ABS_Y means UP.
        servo_value = normalize_axis(-left_y)

        # Right joystick: preserve reversed horizontal mapping.
        stepper_value = normalize_axis(-right_x)

        # Send proportional joystick values.
        message = f"J,{servo_value},{stepper_value}\n"
        send_command(message.encode("ascii"))

        # Process button presses.
        while not events_queue.empty():
            send_command(events_queue.get())

        time.sleep(SEND_INTERVAL)

except KeyboardInterrupt:
    print("\nStopping controller...")

finally:
    if arduino.is_open:
        send_command(b"J,0,0\n")
        time.sleep(0.1)
        arduino.close()

    print("Controller stopped.")