import websockets
import asyncio
import json
import sys
import pickle
import cv2
import signal
import time
from multiprocessing import (
  Lock, Array, Value, RawArray, Process
)
from ctypes import c_int, c_float, c_bool, c_uint8, c_uint16, c_char
import numpy as np
import logging
from datetime import datetime
import qoi

# shared variables
frame_shape = (360, 640)
tx_interval = 1 / 50

frame_lock = Lock()
color_buf = None
depth_buf = None
frame2_lock = Lock()
color2_buf = None
cam2_enable = Value(c_bool, False)
coro_recv = Value(c_int, 0)

motor_values = Array(c_float, 10)
sensor_values = Array(c_float, 20) # [sensor1, sensor2, ...]
sensor_length = Value(c_int, 0)
voltage_level = Value(c_float, 0)

main_loop = None
_running = Value(c_bool, True)
last_rx_time = Array(c_char, 100)
last_rx_time.value = datetime.isoformat(datetime.now()).encode()
comms_task = None

async def try_recv(host, port):
  async with websockets.connect(f"ws://{host}:{port}", max_size=3000000) as websocket:
    h, w = frame_shape
    color_np = np.frombuffer(color_buf, np.uint8).reshape((h, w, 3))
    depth_np = np.frombuffer(depth_buf, np.uint8).reshape((h, w//2, 4))
    color2_np = np.frombuffer(color2_buf, np.uint8).reshape((h, w, 3))

    while _running.value:
      try:
        msg = await asyncio.wait_for(websocket.recv(), timeout=0.5)
        if msg[0] != ord('{') or b'}' not in msg: raise ValueError("")
        frameptr = msg.index(b'}') + 1
        data = json.loads(msg[:frameptr].decode("utf-8"))

        can_write = False

        last_rx_time.acquire()
        prev_time = datetime.fromisoformat(last_rx_time.value.decode())
        curr_time = datetime.fromisoformat(data["timestamp"])
        if curr_time > prev_time:
          can_write = True
          last_rx_time.value = data["timestamp"].encode()
        last_rx_time.release()

        if not can_write: continue

        lengths = data["lengths"]
        if len(lengths) >= 2:
          frameend = frameptr + lengths[0]
          # frame_lock.acquire()
          np.copyto(color_np, cv2.cvtColor(qoi.decode(
            np.frombuffer(msg[frameptr:frameend], dtype=np.uint8)), cv2.COLOR_RGB2BGR))
          frameptr = frameend
          frameend += lengths[1]
          # we just want to copy over the bytes
          np.copyto(depth_np, qoi.decode(
            np.frombuffer(msg[frameptr:frameend], dtype=np.uint8)))
          # frame_lock.release()
          if len(lengths) == 3:
            frameptr = frameend
            frameend += lengths[2]
            # frame2_lock.acquire()
            cam2_enable.value = True
            np.copyto(color2_np, cv2.cvtColor(qoi.decode(
              np.frombuffer(msg[frameptr:frameend], dtype=np.uint8)), cv2.COLOR_RGB2BGR))
            # frame2_lock.release()

        sensor_values.acquire()
        nsensors = sensor_length.value = len(data["sensors"])
        sensor_values[:nsensors] = [float(x) for x in data["sensors"]]
        voltage_level.value = float(data["voltage"]) / 1e3
        sensor_values.release()

      except ValueError:
        logging.error("Invalid data received")
      except asyncio.TimeoutError:
        logging.warning(datetime.isoformat(datetime.now()) + " Connection is jittery, attempting to reset...")
        coro_recv.acquire()
        coro_recv.value = (coro_recv.value + 1) % 4
        coro_recv.release()
        return
      except websockets.ConnectionClosed:
        logging.warning(datetime.isoformat(datetime.now()) + " Connection closed, attempting to reestablish...")
        coro_recv.acquire()
        coro_recv.value = (coro_recv.value + 1) % 4
        coro_recv.release()
        return

async def receive_task(host, port, workerid):
  can_continue = False
  await asyncio.sleep(workerid + 0.1)
  while _running.value:
    coro_recv.acquire() # gets released on failure
    if coro_recv.value == workerid:
      can_continue = True
    else:
      can_continue = False
    coro_recv.release()
    if can_continue:
      await try_recv(host, port - (workerid & 0x1))
      can_continue = False
    else:
      await asyncio.sleep(0.5)

async def sender_task(host, port):
  async with websockets.connect(f"ws://{host}:{port}") as websocket:
    last_tx_time = None
    while _running.value:
      try:
        # throttle communication so that we don't bombard the socket connection
        curr_time = datetime.now()
        dt = tx_interval if last_tx_time is None else (curr_time - last_tx_time).total_seconds()
        if dt < tx_interval:
          await asyncio.sleep(tx_interval - dt)
          last_tx_time = datetime.now()
        else:
          last_tx_time = curr_time
        motor_values.acquire()
        motors = motor_values[:]
        motor_values.release()

        motors = [int(x) for x in np.array(motors, np.float32).clip(-1, 1) * 127]
        msg = json.dumps({"motors": motors}).encode("utf-8")
        await websocket.send(msg)
      except websockets.ConnectionClosed:
        logging.warning(datetime.isoformat(datetime.now()) + " Connection closed, attempting to reestablish...")
        last_tx_time = None
        await asyncio.sleep(1)
        # sys.exit(1)

def comms_worker(host, port, run, cbuf, dbuf, flock, cbuf2, cam2_en, flock2, mvals, svals, ns, vlvl):
  global main_loop, _running
  global color_buf, depth_buf, frame_lock
  global color2_buf, cam2_enable, frame2_lock
  global motor_values, sensor_values, sensor_length, voltage_level
  global coro_recv
  
  color_buf = cbuf
  depth_buf = dbuf
  frame_lock = flock

  color2_buf = cbuf2
  cam2_enable = cam2_en
  frame2_lock = flock2

  motor_values = mvals
  sensor_values = svals
  sensor_length = ns
  voltage_level = vlvl
  coro_recv = Value(c_int, 0)

  _running = run
  main_loop = asyncio.new_event_loop()
  # we need 4 workers to cycle through the 2 network ports, due to instability
  recv_task1 = main_loop.create_task(receive_task(host, port-1, 0))
  recv_task2 = main_loop.create_task(receive_task(host, port-1, 1))
  recv_task3 = main_loop.create_task(receive_task(host, port-1, 2))
  recv_task4 = main_loop.create_task(receive_task(host, port-1, 3))
  send_task = main_loop.create_task(sender_task(host, port))
  try:
    asyncio.set_event_loop(main_loop)
    main_loop.run_until_complete(send_task)
    main_loop.run_until_complete(recv_task1)
    main_loop.run_until_complete(recv_task2)
    main_loop.run_until_complete(recv_task3)
    main_loop.run_until_complete(recv_task4)
  except (KeyboardInterrupt,):
    _running.value = False
    main_loop.stop()
  finally:
    main_loop.run_until_complete(main_loop.shutdown_asyncgens())
    main_loop.close()

def start(host="0.0.0.0", port=9999):
  global comms_task
  global color_buf, depth_buf, color2_buf

  color_buf = RawArray(c_uint8, frame_shape[0] * frame_shape[1] * 3)
  depth_buf = RawArray(c_uint8, frame_shape[0] * frame_shape[1] * 2)
  color2_buf = RawArray(c_uint8, frame_shape[0] * frame_shape[1] * 3)

  comms_task = Process(target=comms_worker, args=(
    host, port, _running,
    color_buf, depth_buf, frame_lock,
    color2_buf, cam2_enable, frame2_lock,
    motor_values, sensor_values, sensor_length, voltage_level))
  comms_task.start()

def stop():
  global comms_task
  was_running = _running.value
  _running.value = False
  if was_running:
    if sys.platform.startswith('win') and comms_task is not None:
      comms_task.terminate()
      comms_task.join()
      comms_task = None
      time.sleep(0.3)
    else:
      comms_task.kill()
      comms_task = None
      time.sleep(0.5) # cant kill the process because linux is weird
      sys.exit(0)

# def sig_handler(signum, frame):
#   if signum == signal.SIGINT or signum == signal.SIGTERM:
#     stop()
#     sys.exit(0)

# signal.signal(signal.SIGINT, sig_handler)
# signal.signal(signal.SIGTERM, sig_handler)

def read():
  """Return sensors values, voltage (V) and time when the data comes in

  Returns:
      Tuple[np.ndarray, np.ndarray, List[int], Dict[float, datetime, np.ndarray]]: color, depth, sensors, { voltage, time, cam2 }
  """
  h, w = frame_shape
  color_np = np.frombuffer(color_buf, np.uint8).reshape((h, w, 3))
  depth_np = np.frombuffer(depth_buf, np.uint16).reshape((h, w))
  # frame_lock.acquire()
  color = np.copy(color_np)
  depth = np.copy(depth_np)
  # frame_lock.release()
  # frame2_lock.acquire()
  if cam2_enable.value:
    color2_np = np.frombuffer(color2_buf, np.uint8).reshape((h, w, 3))
    color2 = np.copy(color2_np)
  else:
    color2 = None
  # frame2_lock.release()

  sensor_values.acquire()
  ns = sensor_length.value
  sensors = [] if ns == 0 else sensor_values[:ns]
  voltage = voltage_level.value
  sensor_values.release()

  last_rx_time.acquire()
  rxtime = last_rx_time.value.decode()
  if len(rxtime) > 0:
    rxtime = datetime.fromisoformat(rxtime)
  else:
    rxtime = None
  last_rx_time.release()

  return color, depth, sensors, { "voltage": voltage, "time": rxtime, "cam2": color2 }

def write(values):
  """Send motor values to remote location

  Args:
      values (List[float]): motor values
  """
  assert(len(values) == 10)
  values = [float(x) for x in values]
  motor_values.acquire()
  motor_values[:] = values
  motor_values.release()

def running():
  return _running.value