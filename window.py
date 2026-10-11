import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from collections import deque
from inputs import get_gamepad
import serial
import threading
import queue
import time
import csv
from datetime import datetime

try:
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False


# ================= CONFIGURATION =================

PORT = "COM3"
BAUD_RATE = 9600

DEADZONE = 8000
UPDATE_MS = 50
TELEMETRY_TIMEOUT_S = 2.0
CONTROLLER_TIMEOUT_S = 2.0
GRAPH_WINDOW_S = 60
MAX_HISTORY = 1200

STEPPER_MIN = 0
STEPPER_MAX = 360
SERVO_MIN = 15
SERVO_MAX = 165

BG = "#10131C"
PANEL = "#1B2231"
BORDER = "#30394D"
TEXT = "#F1F5FF"
MUTED = "#9AA8C2"
BLUE = "#7C9CFF"
GREEN = "#47D7B0"
PURPLE = "#B69AFF"
RED = "#FF7185"
AMBER = "#FFC66D"


# ================= SHARED STATE =================

arduino = None
serial_lock = threading.Lock()
state_lock = threading.Lock()
event_queue = queue.Queue()
running = True

left_y = 0
right_x = 0
last_controller_event = 0.0
controller_error = None

stepper_angle = None
servo_angle = None
solenoid_active = False
last_telemetry_time = 0.0
last_telemetry_line = "No telemetry received yet"
serial_error = None
last_command = None

last_ui_update = time.monotonic()
ui_rate = 0.0

history_t = deque(maxlen=MAX_HISTORY)
history_stepper = deque(maxlen=MAX_HISTORY)
history_servo = deque(maxlen=MAX_HISTORY)
history_csv = deque(maxlen=MAX_HISTORY)


# ================= INPUT HELPERS =================

def normalize_axis(value):
    """Convert a controller axis to a -1000..1000 command."""
    value = max(-32767, min(32767, value))
    magnitude = abs(value)

    if magnitude <= DEADZONE:
        return 0

    scaled = (magnitude - DEADZONE) / (32767 - DEADZONE)
    scaled = max(0.0, min(1.0, scaled))
    result = round(scaled * 1000)

    return result if value >= 0 else -result


# ================= SERIAL HELPERS =================

def open_serial():
    """Attempt to connect without preventing the dashboard from opening."""
    global arduino, serial_error

    try:
        candidate = serial.Serial(PORT, BAUD_RATE, timeout=0.1)
        time.sleep(2)
        candidate.reset_input_buffer()

        with serial_lock:
            arduino = candidate

        serial_error = None
        return True

    except (serial.SerialException, OSError) as error:
        serial_error = str(error)
        arduino = None
        return False


def send_command(command, force=False):
    """Send a newline-terminated command to the updated Arduino sketch."""
    global last_command, serial_error

    if not force and command == last_command:
        return

    try:
        with serial_lock:
            if arduino is not None and arduino.is_open:
                arduino.write(command)
                last_command = command.decode("ascii", errors="ignore")
                serial_error = None
            else:
                serial_error = f"Arduino not connected on {PORT}"

    except (serial.SerialException, OSError) as error:
        serial_error = str(error)


# ================= CONTROLLER READER =================

def read_controller():
    global left_y, right_x, last_controller_event, controller_error

    while running:
        try:
            events = get_gamepad()
            now = time.monotonic()

            with state_lock:
                controller_error = None

                for event in events:
                    if event.ev_type == "Absolute":
                        if event.code == "ABS_Y":
                            left_y = event.state
                            last_controller_event = now

                        elif event.code == "ABS_RX":
                            right_x = event.state
                            last_controller_event = now

                    elif event.ev_type == "Key":
                        # Xbox X button activates the solenoid.
                        if event.code == "BTN_WEST" and event.state == 1:
                            event_queue.put(("button", "X"))

                        # Xbox B button resets the turret.
                        elif event.code == "BTN_EAST" and event.state == 1:
                            left_y = 0
                            right_x = 0
                            event_queue.put(("button", "B"))

                        last_controller_event = now

        except Exception as error:
            if running:
                with state_lock:
                    controller_error = str(error)
                    left_y = 0
                    right_x = 0
                    last_controller_event = 0.0

                event_queue.put(("controller_error", str(error)))
                time.sleep(0.25)


# ================= ARDUINO TELEMETRY READER =================

def read_arduino():
    global stepper_angle, servo_angle, solenoid_active
    global last_telemetry_time, last_telemetry_line, serial_error

    while running:
        if arduino is None or not getattr(arduino, "is_open", False):
            time.sleep(0.5)
            continue

        try:
            line = arduino.readline().decode(
                "utf-8", errors="ignore"
            ).strip()

            if not line:
                continue

            # Expected format:
            # POS,stepper_angle,servo_angle,solenoid_state
            if line.startswith("POS,"):
                parts = line.split(",")

                if len(parts) == 4:
                    step = float(parts[1])
                    servo = float(parts[2])
                    solenoid = parts[3].strip() == "1"
                    now = time.monotonic()

                    with state_lock:
                        stepper_angle = step
                        servo_angle = servo
                        solenoid_active = solenoid
                        last_telemetry_time = now
                        last_telemetry_line = line
                        serial_error = None

                    history_csv.append((
                        datetime.now().isoformat(timespec="milliseconds"),
                        step,
                        servo,
                        int(solenoid)
                    ))

        except (ValueError, serial.SerialException, OSError) as error:
            if running:
                serial_error = str(error)
                event_queue.put(("serial_error", str(error)))
                time.sleep(0.2)


# ================= WINDOW =================

root = tk.Tk()
root.title("Motion Control | Robotics Dashboard")
root.geometry("1180x900")
root.minsize(900, 700)
root.configure(bg=BG)

style = ttk.Style()
style.theme_use("clam")
style.configure(
    "TProgressbar",
    troughcolor=BORDER,
    background=BLUE,
    bordercolor=BORDER,
    lightcolor=BLUE,
    darkcolor=BLUE,
    thickness=8
)
style.configure(
    "TButton",
    font=("Arial", 9, "bold"),
    padding=7
)

# Scrollable dashboard.
outer = tk.Frame(root, bg=BG)
outer.pack(fill="both", expand=True)

canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
scrollbar = ttk.Scrollbar(
    outer, orient="vertical", command=canvas.yview
)
canvas.configure(yscrollcommand=scrollbar.set)

scrollbar.pack(side="right", fill="y")
canvas.pack(side="left", fill="both", expand=True)

main = tk.Frame(canvas, bg=BG, padx=20, pady=18)
window_id = canvas.create_window(
    (0, 0), window=main, anchor="nw"
)


def update_scroll_region(_event=None):
    canvas.configure(scrollregion=canvas.bbox("all"))
    canvas.itemconfigure(window_id, width=canvas.winfo_width())


main.bind("<Configure>", update_scroll_region)
canvas.bind("<Configure>", update_scroll_region)


def make_card(parent, padding=14):
    return tk.Frame(
        parent,
        bg=PANEL,
        padx=padding,
        pady=padding,
        highlightbackground=BORDER,
        highlightthickness=1
    )


# ================= HEADER =================

header = tk.Frame(main, bg=BG)
header.pack(fill="x", pady=(0, 8))

tk.Label(
    header,
    text="◉  MOTION CONTROL",
    font=("Arial", 22, "bold"),
    bg=BG,
    fg=TEXT
).pack(side="left")

status_label = tk.Label(
    header,
    text="● CHECKING CONNECTIONS",
    font=("Arial", 10, "bold"),
    bg=BG,
    fg=AMBER
)
status_label.pack(side="right", pady=8)

tk.Label(
    main,
    text="REAL-TIME HARDWARE MONITOR  /  TELEMETRY + INPUT DIAGNOSTICS",
    font=("Arial", 9, "bold"),
    bg=BG,
    fg=MUTED
).pack(anchor="w", pady=(0, 14))


# ================= MOTOR CARDS =================

cards = tk.Frame(main, bg=BG)
cards.pack(fill="x")


def create_motor_card(
    parent, title, subtitle, accent, minimum, maximum
):
    card = make_card(parent, 16)
    card.pack(
        side="left",
        fill="both",
        expand=True,
        padx=(0, 8)
    )

    tk.Label(
        card,
        text=title,
        font=("Arial", 12, "bold"),
        bg=PANEL,
        fg=accent
    ).pack(anchor="w")

    tk.Label(
        card,
        text=subtitle,
        font=("Arial", 9),
        bg=PANEL,
        fg=MUTED
    ).pack(anchor="w", pady=(3, 10))

    angle_label = tk.Label(
        card,
        text="—°",
        font=("Arial", 30, "bold"),
        bg=PANEL,
        fg=TEXT
    )
    angle_label.pack(anchor="w")

    bar = ttk.Progressbar(
        card,
        maximum=maximum - minimum,
        mode="determinate"
    )
    bar.pack(fill="x", pady=(12, 8))

    tk.Label(
        card,
        text=f"{minimum}° — {maximum}°",
        font=("Arial", 9),
        bg=PANEL,
        fg=MUTED
    ).pack(anchor="w")

    direction = tk.Label(
        card,
        text="● WAITING FOR DATA",
        font=("Arial", 9, "bold"),
        bg=PANEL,
        fg=MUTED
    )
    direction.pack(anchor="w", pady=(10, 0))

    return angle_label, bar, direction


stepper_widgets = create_motor_card(
    cards,
    "01  STEPPER MOTOR",
    "28BYJ-48 · ULN2003 DRIVER",
    BLUE,
    STEPPER_MIN,
    STEPPER_MAX
)

servo_widgets = create_motor_card(
    cards,
    "02  SERVO MOTOR",
    "COMMANDED ANGLE · NOT SENSOR MEASURED",
    GREEN,
    SERVO_MIN,
    SERVO_MAX
)


# ================= CONTROLLER + DIAGNOSTICS =================

second_row = tk.Frame(main, bg=BG)
second_row.pack(fill="x", pady=12)

controller_card = make_card(second_row)
controller_card.pack(
    side="left",
    fill="both",
    expand=True,
    padx=(0, 8)
)

tk.Label(
    controller_card,
    text="XBOX CONTROLLER INPUT",
    font=("Arial", 11, "bold"),
    bg=PANEL,
    fg=PURPLE
).pack(anchor="w")

tk.Label(
    controller_card,
    text="Live stick positions · deadzone visualization",
    font=("Arial", 9),
    bg=PANEL,
    fg=MUTED
).pack(anchor="w", pady=(3, 6))

joystick_canvas = tk.Canvas(
    controller_card,
    width=360,
    height=210,
    bg=PANEL,
    highlightthickness=0
)
joystick_canvas.pack(
    fill="both",
    expand=True,
    pady=(0, 4)
)


def draw_joystick(
    cx, cy, radius, x_value, y_value, label, color
):
    joystick_canvas.create_oval(
        cx-radius,
        cy-radius,
        cx+radius,
        cy+radius,
        outline=BORDER,
        width=2
    )

    joystick_canvas.create_line(
        cx-radius, cy, cx+radius, cy, fill=BORDER
    )
    joystick_canvas.create_line(
        cx, cy-radius, cx, cy+radius, fill=BORDER
    )

    deadzone_radius = radius * DEADZONE / 32767

    joystick_canvas.create_oval(
        cx-deadzone_radius,
        cy-deadzone_radius,
        cx+deadzone_radius,
        cy+deadzone_radius,
        outline="#56627A",
        dash=(3, 3)
    )

    px = cx + max(-1, min(1, x_value / 32767)) * radius
    py = cy + max(-1, min(1, y_value / 32767)) * radius

    joystick_canvas.create_oval(
        px-7,
        py-7,
        px+7,
        py+7,
        fill=color,
        outline=TEXT,
        width=1
    )

    joystick_canvas.create_text(
        cx,
        cy+radius+15,
        text=label,
        fill=MUTED,
        font=("Arial", 9, "bold")
    )


controller_values_label = tk.Label(
    controller_card,
    text="Left Y: 0     Right X: 0",
    font=("Courier", 9),
    bg=PANEL,
    fg=TEXT
)
controller_values_label.pack(anchor="w", pady=(4, 0))

diagnostics_card = make_card(second_row)
diagnostics_card.pack(side="left", fill="both", expand=True)

tk.Label(
    diagnostics_card,
    text="CONNECTION & DIAGNOSTICS",
    font=("Arial", 11, "bold"),
    bg=PANEL,
    fg=TEXT
).pack(anchor="w", pady=(0, 10))


def make_status_row(parent, label_text):
    row = tk.Frame(parent, bg=PANEL)
    row.pack(fill="x", pady=4)

    tk.Label(
        row,
        text=label_text,
        font=("Arial", 9),
        bg=PANEL,
        fg=MUTED
    ).pack(side="left")

    value = tk.Label(
        row,
        text="CHECKING",
        font=("Arial", 9, "bold"),
        bg=PANEL,
        fg=TEXT
    )
    value.pack(side="right")

    return value


arduino_status = make_status_row(
    diagnostics_card, "Arduino serial"
)
controller_status = make_status_row(
    diagnostics_card, "Xbox controller"
)
telemetry_status = make_status_row(
    diagnostics_card, "Last valid telemetry"
)
controller_age_status = make_status_row(
    diagnostics_card, "Last controller input"
)
port_status = make_status_row(
    diagnostics_card, "Port / baud rate"
)
update_rate_status = make_status_row(
    diagnostics_card, "Dashboard refresh"
)
solenoid_status = make_status_row(
    diagnostics_card, "Solenoid state"
)

diagnostic_message = tk.Label(
    diagnostics_card,
    text="Starting diagnostics...",
    font=("Arial", 9),
    bg=PANEL,
    fg=MUTED,
    justify="left",
    wraplength=420,
    anchor="w"
)
diagnostic_message.pack(fill="x", pady=(10, 0))


# ================= LIVE TELEMETRY GRAPH =================

graph_card = make_card(main)
graph_card.pack(fill="both", expand=True, pady=(0, 12))

graph_header = tk.Frame(graph_card, bg=PANEL)
graph_header.pack(fill="x")

tk.Label(
    graph_header,
    text="LIVE TELEMETRY",
    font=("Arial", 11, "bold"),
    bg=PANEL,
    fg=TEXT
).pack(side="left")

tk.Label(
    graph_header,
    text=f"ANGLE vs TIME · LAST {GRAPH_WINDOW_S}s",
    font=("Arial", 9, "bold"),
    bg=PANEL,
    fg=MUTED
).pack(side="right")

graph_ax = None
graph_widget = None
stepper_line = None
servo_line = None

if MATPLOTLIB_AVAILABLE:
    figure = Figure(
        figsize=(8, 3.2),
        dpi=100,
        facecolor=PANEL
    )

    graph_ax = figure.add_subplot(111)
    graph_ax.set_facecolor(PANEL)
    graph_ax.set_xlabel("Time (seconds)", color=MUTED)
    graph_ax.set_ylabel("Angle (degrees)", color=MUTED)
    graph_ax.tick_params(colors=MUTED, labelsize=8)

    for spine in graph_ax.spines.values():
        spine.set_color(BORDER)

    graph_ax.grid(True, color=BORDER, alpha=0.65)

    stepper_line, = graph_ax.plot(
        [], [], color=BLUE, linewidth=2, label="Stepper"
    )
    servo_line, = graph_ax.plot(
        [], [], color=GREEN, linewidth=2, label="Servo"
    )

    legend = graph_ax.legend(
        facecolor=PANEL,
        edgecolor=BORDER
    )

    for label in legend.get_texts():
        label.set_color(TEXT)

    figure.tight_layout()

    graph_widget = FigureCanvasTkAgg(
        figure, master=graph_card
    )
    graph_widget.get_tk_widget().pack(
        fill="both",
        expand=True,
        pady=(8, 0)
    )

else:
    tk.Label(
        graph_card,
        text=(
            "Live graph unavailable: install Matplotlib with\n"
            "python -m pip install matplotlib"
        ),
        font=("Arial", 10),
        bg=PANEL,
        fg=AMBER,
        justify="left"
    ).pack(anchor="w", pady=16)

graph_buttons = tk.Frame(graph_card, bg=PANEL)
graph_buttons.pack(fill="x", pady=(8, 0))

graph_paused = False


def toggle_graph():
    global graph_paused

    graph_paused = not graph_paused
    graph_pause_button.config(
        text="Resume graph" if graph_paused else "Pause graph"
    )


def clear_graph():
    history_t.clear()
    history_stepper.clear()
    history_servo.clear()

    if graph_ax is not None:
        stepper_line.set_data([], [])
        servo_line.set_data([], [])
        graph_ax.set_xlim(-GRAPH_WINDOW_S, 0)
        graph_ax.set_ylim(0, 360)
        graph_widget.draw_idle()


graph_pause_button = ttk.Button(
    graph_buttons,
    text="Pause graph",
    command=toggle_graph
)
graph_pause_button.pack(side="left", padx=(0, 6))

ttk.Button(
    graph_buttons,
    text="Clear graph",
    command=clear_graph
).pack(side="left")

ttk.Button(
    graph_buttons,
    text="Export CSV",
    command=lambda: export_csv()
).pack(side="right")


# ================= EVENT LOG =================

log_card = make_card(main)
log_card.pack(fill="x", pady=(0, 12))

log_header = tk.Frame(log_card, bg=PANEL)
log_header.pack(fill="x")

tk.Label(
    log_header,
    text="EVENT LOG",
    font=("Arial", 11, "bold"),
    bg=PANEL,
    fg=TEXT
).pack(side="left")

log_text = tk.Text(
    log_card,
    height=5,
    bg="#121722",
    fg=MUTED,
    insertbackground=TEXT,
    relief="flat",
    font=("Courier", 9),
    wrap="word",
    state="disabled"
)
log_text.pack(fill="x", pady=(8, 0))


def log_event(message, color=MUTED):
    timestamp = datetime.now().strftime("%H:%M:%S")

    log_text.configure(state="normal")
    log_text.insert(
        "end",
        f"[{timestamp}] {message}\n"
    )
    log_text.see("end")

    line_count = int(log_text.index("end-1c").split(".")[0])

    if line_count > 150:
        log_text.delete("1.0", f"{line_count-150}.0")

    log_text.configure(state="disabled")


def export_csv():
    if not history_csv:
        messagebox.showinfo(
            "No telemetry",
            "No valid telemetry has been received to export yet."
        )
        return

    path = filedialog.asksaveasfilename(
        title="Export telemetry",
        defaultextension=".csv",
        filetypes=[("CSV files", "*.csv")]
    )

    if not path:
        return

    try:
        with open(path, "w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow([
                "timestamp",
                "stepper_angle_deg",
                "servo_angle_deg",
                "solenoid_active"
            ])
            writer.writerows(list(history_csv))

        log_event(f"Telemetry exported to {path}")

    except OSError as error:
        messagebox.showerror("Export failed", str(error))


# ================= CONTROLS / STATUS MESSAGE =================

guide = tk.Label(
    main,
    text=(
        "CONTROLS  ·  LEFT STICK ↑/↓ = SERVO  ·  "
        "RIGHT STICK ←/→ = STEPPER  ·  "
        "X = SOLENOID  ·  B = RESET POSITION"
    ),
    font=("Arial", 9, "bold"),
    bg=BG,
    fg=PURPLE,
    anchor="w"
)
guide.pack(fill="x", pady=(0, 5))

message_label = tk.Label(
    main,
    text="Waiting for Arduino position telemetry...",
    font=("Arial", 9),
    bg=BG,
    fg=MUTED,
    anchor="w"
)
message_label.pack(fill="x", pady=(0, 4))


# ================= UI UPDATE =================

def update_dashboard():
    global running, last_ui_update, ui_rate

    if not running:
        return

    now = time.monotonic()
    elapsed = now - last_ui_update
    last_ui_update = now

    if elapsed > 0:
        instant_rate = 1.0 / elapsed
        ui_rate = (
            instant_rate
            if ui_rate == 0
            else 0.8 * ui_rate + 0.2 * instant_rate
        )

    with state_lock:
        current_left_y = left_y
        current_right_x = right_x
        controller_seen = last_controller_event
        current_controller_error = controller_error
        step_angle = stepper_angle
        servo_angle_value = servo_angle
        solenoid = solenoid_active
        telemetry_seen = last_telemetry_time
        telemetry_line = last_telemetry_line

    # Updated Arduino protocol:
    # J,servoInput,stepperInput
    #
    # Servo:
    #   Positive = up
    #   Negative = down
    #
    # Stepper:
    #   Horizontal direction is intentionally reversed.
    servo_input = normalize_axis(-current_left_y)
    stepper_input = normalize_axis(-current_right_x)

    command = (
        f"J,{servo_input},{stepper_input}\n"
    ).encode("ascii")

    send_command(command)

    # Process button presses.
    while True:
        try:
            event = event_queue.get_nowait()
        except queue.Empty:
            break

        if not isinstance(event, tuple):
            continue

        if event[0] == "button" and event[1] in ("X", "B"):
            button = event[1]

            try:
                with serial_lock:
                    if arduino is not None and arduino.is_open:
                        arduino.write(
                            f"{button}\n".encode("ascii")
                        )

                        if button == "X":
                            log_event(
                                "X button: solenoid activation command sent",
                                AMBER
                            )
                        else:
                            log_event(
                                "B button: turret reset requested "
                                "(horizontal 0°, vertical 90°)",
                                PURPLE
                            )
                    else:
                        log_event(
                            f"{button} button pressed but Arduino is disconnected",
                            RED
                        )

            except (serial.SerialException, OSError) as error:
                log_event(
                    f"{button} command failed: {error}",
                    RED
                )

        elif event[0] == "controller_error":
            log_event(
                f"Controller error: {event[1]}",
                RED
            )

        elif event[0] == "serial_error":
            log_event(
                f"Serial error: {event[1]}",
                RED
            )

    # ================= STEPPER DISPLAY =================

    if step_angle is not None:
        shown = max(
            STEPPER_MIN,
            min(STEPPER_MAX, step_angle)
        )

        stepper_widgets[0].config(text=f"{shown:.1f}°")
        stepper_widgets[1]["value"] = shown - STEPPER_MIN

        if stepper_input < 0:
            direction = "LEFT"
        elif stepper_input > 0:
            direction = "RIGHT"
        else:
            direction = "STOPPED"

        stepper_widgets[2].config(
            text=f"● {direction}",
            fg=BLUE if direction != "STOPPED" else MUTED
        )

    else:
        stepper_widgets[0].config(text="—°")
        stepper_widgets[2].config(
            text="● WAITING FOR DATA",
            fg=MUTED
        )

    # ================= SERVO DISPLAY =================

    if servo_angle_value is not None:
        shown_servo = max(
            SERVO_MIN,
            min(SERVO_MAX, servo_angle_value)
        )

        servo_widgets[0].config(
            text=f"{shown_servo:.1f}°"
        )
        servo_widgets[1]["value"] = shown_servo - SERVO_MIN

        if servo_input > 0:
            direction = "UP"
        elif servo_input < 0:
            direction = "DOWN"
        else:
            direction = "STOPPED"

        servo_widgets[2].config(
            text=f"● {direction}",
            fg=GREEN if direction != "STOPPED" else MUTED
        )

    else:
        servo_widgets[0].config(text="—°")
        servo_widgets[2].config(
            text="● WAITING FOR DATA",
            fg=MUTED
        )

    # ================= JOYSTICK VISUALIZATION =================

    joystick_canvas.delete("all")

    width = max(300, joystick_canvas.winfo_width())
    height = max(190, joystick_canvas.winfo_height())

    radius = min(58, max(42, height * 0.28))
    left_cx = width * 0.27
    right_cx = width * 0.73
    cy = height * 0.42

    # ABS_Y is negative when the left stick is pushed upward.
    # Invert it for the canvas visualization.
    draw_joystick(
        left_cx,
        cy,
        radius,
        0,
        -current_left_y,
        "LEFT STICK · Y",
        GREEN
    )

    draw_joystick(
        right_cx,
        cy,
        radius,
        current_right_x,
        0,
        "RIGHT STICK · X",
        BLUE
    )

    controller_values_label.config(
        text=(
            f"Left Y: {current_left_y:+6d}     "
            f"Right X: {current_right_x:+6d}"
        )
    )

    # ================= CONNECTION STATUS =================

    telemetry_age = (
        now - telemetry_seen if telemetry_seen else None
    )
    controller_age = (
        now - controller_seen if controller_seen else None
    )

    serial_connected = (
        telemetry_age is not None
        and telemetry_age <= TELEMETRY_TIMEOUT_S
    )

    controller_connected = (
        controller_age is not None
        and controller_age <= CONTROLLER_TIMEOUT_S
        and not current_controller_error
    )

    arduino_status.config(
        text=(
            "RESPONDING"
            if serial_connected
            else "NO RECENT TELEMETRY"
        ),
        fg=GREEN if serial_connected else RED
    )

    controller_status.config(
        text=(
            "INPUT RECEIVED"
            if controller_connected
            else (
                "ERROR"
                if current_controller_error
                else "WAITING / STALE"
            )
        ),
        fg=GREEN if controller_connected else RED
    )

    telemetry_status.config(
        text=(
            f"{telemetry_age:.1f}s ago"
            if telemetry_age is not None
            else "Never"
        ),
        fg=GREEN if serial_connected else RED
    )

    controller_age_status.config(
        text=(
            f"{controller_age:.1f}s ago"
            if controller_age is not None
            else "Never"
        ),
        fg=GREEN if controller_connected else AMBER
    )

    port_status.config(text=f"{PORT}  /  {BAUD_RATE}")
    update_rate_status.config(
        text=f"{ui_rate:.1f} UI updates/s"
    )

    solenoid_status.config(
        text="ACTIVE" if solenoid else "INACTIVE",
        fg=RED if solenoid else GREEN
    )

    if serial_connected and controller_connected:
        status_label.config(
            text="● SYSTEM ONLINE",
            fg=GREEN
        )
    elif serial_connected:
        status_label.config(
            text="● ARDUINO ONLINE · CONTROLLER CHECK",
            fg=AMBER
        )
    else:
        status_label.config(
            text="● CONNECTION ISSUE",
            fg=RED
        )

    if current_controller_error:
        diagnostic_message.config(
            text=f"Controller: {current_controller_error}",
            fg=RED
        )

    elif serial_error:
        diagnostic_message.config(
            text=f"Serial: {serial_error}",
            fg=RED
        )

    elif not serial_connected:
        diagnostic_message.config(
            text=(
                f"No valid POS telemetry in the last "
                f"{TELEMETRY_TIMEOUT_S:.0f}s. Check {PORT}, "
                "baud rate, wiring, and Arduino telemetry format."
            ),
            fg=AMBER
        )

    else:
        diagnostic_message.config(
            text=f"Latest Arduino line: {telemetry_line}",
            fg=MUTED
        )

    # ================= GRAPH HISTORY =================

    if (
        telemetry_seen
        and step_angle is not None
        and servo_angle_value is not None
    ):
        if not history_t or telemetry_seen != history_t[-1]:
            history_t.append(telemetry_seen)
            history_stepper.append(step_angle)
            history_servo.append(servo_angle_value)

    if (
        MATPLOTLIB_AVAILABLE
        and not graph_paused
        and history_t
    ):
        cutoff = now - GRAPH_WINDOW_S

        visible_data = [
            (t, step, servo)
            for t, step, servo in zip(
                history_t,
                history_stepper,
                history_servo
            )
            if t >= cutoff
        ]

        xs = [t - now for t, _, _ in visible_data]
        steps = [step for _, step, _ in visible_data]
        servos = [servo for _, _, servo in visible_data]

        stepper_line.set_data(xs, steps)
        servo_line.set_data(xs, servos)

        graph_ax.set_xlim(-GRAPH_WINDOW_S, 0)

        visible_angles = steps + servos

        if visible_angles:
            low = min(visible_angles)
            high = max(visible_angles)
            padding = max(10, (high - low) * 0.15)

            graph_ax.set_ylim(
                max(-10, low - padding),
                min(370, high + padding)
            )

        graph_widget.draw_idle()

    root.after(UPDATE_MS, update_dashboard)


# ================= SHUTDOWN =================

def close_app():
    global running

    running = False

    try:
        with serial_lock:
            if arduino is not None and arduino.is_open:
                # Stop both motors using the new protocol.
                arduino.write(b"J,0,0\n")
                arduino.close()

    except (serial.SerialException, OSError):
        pass

    root.destroy()


# ================= STARTUP =================

if open_serial():
    log_event(
        f"Opened serial port {PORT} at {BAUD_RATE} baud"
    )
else:
    log_event(
        f"Could not open {PORT}: {serial_error}",
        RED
    )

threading.Thread(
    target=read_controller,
    daemon=True
).start()

threading.Thread(
    target=read_arduino,
    daemon=True
).start()

root.protocol("WM_DELETE_WINDOW", close_app)
root.after(UPDATE_MS, update_dashboard)
root.mainloop()