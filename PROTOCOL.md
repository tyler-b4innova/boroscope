# Teslong TD300 / ZMFICamera MFi Protocol Specification

Reverse engineered from:
`/Applications/Smart Endoscope.app/Wrapper/Runner.app/Frameworks/ZMFICamera.framework/ZMFICamera`
ARM64 Mach-O, built from source at `/Users/jinbo/Documents/SVN/Project/zCamera/mficamera/mficamera.m`

---

## 1. Transport Layer

Communication uses Apple MFi (Made for iPhone) External Accessory protocol over
USB/Lightning. The Objective-C class `vszCameraSessionController` manages the
EASession with `inputStream` / `outputStream` (NSStream).

The protocol name is configured via `iConfig.protocolName` set during `openSession`.

All writes go through `_vs_write_data_to_device(data_ptr, length)`, which acquires
a mutex lock, calls the internal `__vs_write_data_to_device`, then unlocks.

All reads happen on a dedicated thread (`_zCamera_read_thread`) that polls in a
loop with 1ms (`usleep(1000)`) sleeps, reading into a 2048-byte (0x800) buffer
allocated by `mfiCameraInit`.

---

## 2. Packet Format

### 2.1 Command / Response Header (5 bytes minimum)

Every packet (both sent commands and received responses) uses this header:

```
Offset  Size   Field         Description
------  -----  -----------   ----------------------------------------
0x00    1      sync_hi       Sync byte high: always 0xBB or 0xAA
0x01    1      sync_lo       Sync byte low:  always 0xAA or 0xBB
0x02    1      cid           Command ID (identifies the command type)
0x03    2      length        Payload length (little-endian uint16)
0x05    N      payload       Command-specific payload (length bytes)
```

**Total packet size = 5 + length**

### 2.2 Sync Word Detection

The `_check_data_ok` function recognizes two valid sync byte orderings:

| Byte 0 | Byte 1 | Meaning                      |
|--------|--------|------------------------------|
| `0xAA` | `0xBB` | Valid sync (host -> device?)  |
| `0xBB` | `0xAA` | Valid sync (device -> host?)  |

Both orderings are accepted. The firmware uses `0xBB 0xAA` for outgoing
commands (based on the constant data at `0x16040`). The check logic also handles
partial sync bytes at buffer boundaries (single-byte `0xAA` or `0xBB` at end
of buffer is treated as an incomplete header needing more data).

### 2.3 Packet Length Calculation

From `_get_pro_len`:
```c
uint16_t get_pro_len(uint8_t *buf, int buf_len) {
    uint16_t payload_len = *(uint16_t *)(buf + 3);  // little-endian halfword at offset 3
    return (payload_len + 5) & 0xFFFF;               // total packet = header(5) + payload
}
```

### 2.4 Maximum Packet Size

The `_check_data_ok` function enforces a maximum total packet size of **2048
bytes** (0x800). If `length + 5 > 0x800`, the packet is rejected (return code 4
= "packet too large").

---

## 3. Command IDs (CID)

Extracted from `_handle_pro` dispatch logic and `_check_cmd_return` usage:

| CID  | Name                | Direction       | Description                        |
|------|---------------------|-----------------|------------------------------------|
| 0x05 | CID_GET_ALLINFO     | Response        | Device info / capabilities reply   |
| 0x06 | CID_OPEN_STREAM     | Response        | Open camera stream reply           |
| 0x07 | CID_VIDEO_DATA      | Response        | Video frame data (JPEG stream)     |
| 0x08 | CID_SLEEP           | Command         | Put camera to sleep                |
| 0x0A | CID_VIDEO_DATA_ALT  | Response        | Alternate video data CID           |
| 0x0B | CID_SWITCH_CAMERA   | Both            | Switch camera / change resolution  |

### CID dispatch in `_handle_pro`:

```
if (pro->cid == 0x07 || pro->cid == 0x0A):
    -> Video frame data handler (JPEG image accumulation)
elif (pro->cid == 0x05):
    -> CID_GET_ALLINFO handler (device info, 0x62 bytes payload)
elif (pro->cid == 0x06):
    -> CID_OPEN_STREAM handler (stream opened acknowledgment)
elif (pro->cid == 0x0B):
    -> CID_SWITCH_CAMERA handler
else:
    -> Log unknown "pro->cid=%d"
```

---

## 4. Commands (Host -> Device)

All commands are encoded as constant byte arrays in the `__TEXT` segment at
offset `0x16040`. Each is exactly **5 bytes** (header only, zero-length payload).

### 4.1 Command Constants Table

```
Address   Bytes              CID   Payload  Name
-------   -----------------  ----  -------  -------------------------
0x16040   BB AA 0B 03 00     0x0B  0x0003   changeCamera (8-byte cmd)
0x16048   BB AA 06 00 00     0x06  0x0000   sleepCamera (devflag==1)
0x1604D   BB AA 08 00 00     0x08  0x0000   sleepCamera (devflag==2)
0x16052   BB AA 05 00 00     0x05  0x0000   wakeCamera (devflag==1)
0x16057   BB AA 06 00 00     0x06  0x0000   wakeCamera (devflag==2)
0x1605C   BB AA 05 00 00     0x05  0x0000   opentypepro (get dev info)
0x16061   BB AA 06 00 00     0x06  0x0000   opencamerapro (open stream)
```

### 4.2 openCamera Sequence

`openCamera` is the main function to start the video stream. It performs a
**two-step** sequence:

```
Step 1: begincmd()
Step 2: opentypepro  -> sends [BB AA 05 00 00]  (CID=0x05, "get device type/info")
         waits for response with check_cmd_return(0x05)
         if devflag becomes 1 -> device responded, proceed to step 3
         if check_cmd_return returns 1 -> return 2 (type confirmed)
Step 3: opencamerapro -> sends [BB AA 06 00 00]  (CID=0x06, "open camera stream")
         waits for response with check_cmd_return(0x06)
Step 4: endcmd()
```

**Polling loop**: After sending each command, the code polls up to **100 times**
with **10ms** (`usleep(10000)`) delays between each check, calling
`check_cmd_return(expected_cid)` which examines the response buffer at
`0x22aea` (a 1024-byte buffer) to see if a response with matching CID arrived
and the status byte at offset 5 is 0 (success).

### 4.3 sleepCamera

Depends on `devflag` state variable:
- **devflag == 1**: sends `[BB AA 06 00 00]` (CID=0x06)
- **devflag == 2**: sends `[BB AA 08 00 00]` (CID=0x08)

### 4.4 wakeCamera

Depends on `devflag` state variable:
- **devflag == 1**: sends `[BB AA 05 00 00]` (CID=0x05)
- **devflag == 2**: sends `[BB AA 06 00 00]` (CID=0x06)

### 4.5 changeCamera (Switch Camera / Resolution)

This is the most complex command. It sends an **8-byte** packet:

```
[BB AA 0B 03 00] [cam_id] [resolution_le16]
 ^--- header ---^  ^----- 3-byte payload -----^
```

- `cam_id` (1 byte): camera index to switch to (arg w0)
- `resolution` (2 bytes, little-endian): resolution selector (arg w1)

After sending, it calls `setfilernum(0x0F)` and polls `check_cmd_return(0x0B)`
up to **300 times** (0x12C) with 10ms delays (total timeout: 3 seconds).

---

## 5. Response Processing

### 5.1 Data Reception Pipeline

```
_zCamera_read_thread
  -> reads from EASession inputStream
  -> calls _mfi_handle_data(buf, len)
       -> acquires mutex
       -> calls _mfi_handle_data2(buf, len)
            -> accumulates data in 2048-byte buffer
            -> calls _check_data_ok() to detect packet boundaries
            -> calls _get_pro_len() for packet length
            -> calls _handle_pro() for complete packets
       -> releases mutex
```

### 5.2 Packet State Machine (`_check_data_ok` return codes)

| Code | Meaning                                  |
|------|------------------------------------------|
| 1    | Accumulating: single sync byte found     |
| 2    | Scanning: skip non-sync byte             |
| 3    | Incomplete: need more data for payload   |
| 4    | Error: packet exceeds 2048-byte limit    |
| 5    | Complete: full packet available           |

### 5.3 `_handle_pro` Response Processing

Called when a complete packet is detected. The function at `0xf698`:

1. **Reads CID** from `pro[2]` (byte offset 2)
2. **Checks for video data**: CID 0x07 or 0x0A
3. **Sets devflag**: If devflag==0 and CID==0x07, sets devflag=1
4. **Extracts sub-header** at `pro + 5` (the "inner protocol header"):
   - Byte `pro[5+0]`: frame sequence number
   - Byte `pro[5+2]`: button/status flags (bits extracted individually)
     - Bit 0: picbutton (photo capture button pressed)
     - Bit 1: zoomUp
     - Bit 2: zoomDown
     - Bit 3: (unused or reserved)
     - Bit 4: (unused or reserved)
   - Bytes `pro[5+3..5+6]`: accelerometer/angle raw data (32-bit)

5. **Frame assembly** (for CID 0x07/0x0A):
   - Checks if sequence number matches expected
   - Creates a `mu_camera_data` structure to accumulate JPEG data
   - Calls `mu_camera_data_add(data_obj, payload_ptr, payload_len, max_size)`
   - Frame payload starts at `pro + 5 + 7` (12 bytes into the packet)
   - Frame data length is `pro_length - 7`
   - JPEG completion detected by `_checkend()` looking for `FF D9` marker

6. **Device info** (CID 0x05, "CID_GET_ALLINFO"):
   - Payload is 0x62 (98) bytes: `vs_devinfo_t` structure
   - Copied to global at `0x22a88`
   - Sets devflag = 2
   - Structure layout (from log strings):
     ```
     Offset   Field
     0x00     fw_info.vendor   (16-byte string)
     0x10     fw_info.product  (16-byte string)
     0x20     fw_info.version  (16-byte string)
     0x4E     lic_info.valid   (byte)
     ...      cam_info.cam_num
     ...      cam_info.cam_cur
     ...      cam_info.res_cur
     ...      cam_info.res_list[]
     ...      capacity
     ...      version
     ```

7. **Open stream ack** (CID 0x06):
   - Copies response to buffer at `0x22aea` (up to 0x400 bytes)
   - Stores response length at `0x22eec`
   - The `_check_cmd_return` function reads `buf[2]` for CID match and `buf[5]` for status==0

8. **Switch camera ack** (CID 0x0B):
   - Logs `pro_rt->length`, `cam_num`, `cam_res`
   - Response payload at offset 6: `cam_num` at byte 0, `cam_res` at byte 1
   - Also copies to `0x22aea` buffer like CID 0x06

---

## 6. Video Frame Format

### 6.1 Frame Wrapper

Video frames arrive as CID 0x07 (or 0x0A) packets with this inner structure:

```
Offset   Size   Field
------   ----   -----
0x00     1      seq_num       Frame sequence number (wrapping byte)
0x01     1      (reserved)
0x02     1      flags         Button/sensor status bits
0x03     4      angle_data    Raw accelerometer/gyroscope data (32-bit LE)
0x07     N      jpeg_data     JPEG image fragment
```

Total inner-header: 7 bytes.
JPEG data starts at packet offset 12 (5 byte outer header + 7 byte inner header).

### 6.2 JPEG Reassembly

Frames may span multiple packets. The firmware:
1. Starts a new JPEG when it sees a frame with `0xFF 0xD8` at the start
2. Accumulates fragments across packets by matching sequence numbers
3. Completes a frame when `_checkend()` finds `0xFF 0xD9` (JPEG EOI marker)
   at the end of the accumulated data (trailing zero bytes are skipped)
4. Max frame buffer: initial allocation of 0x800 bytes, grown by `mu_camera_data_add`

### 6.3 Frame Delivery

Completed JPEG frames are pushed to a `mucamera` queue. The `_data_thread`
function (at `0xe3f8`) pulls frames from this queue and:
1. Extracts button flags from the `0x2c` byte of the camera data structure
2. Extracts angle data from offset `0x28` (4 bytes)
3. Calls `_getangleyz()` if picbutton flag is set (bit 0)
4. Delivers to the Objective-C delegate via `receiveCameraData:`

---

## 7. Global Variables

| Address  | Name            | Type      | Description                           |
|----------|-----------------|-----------|---------------------------------------|
| 0x1e958  | _g512CBW        | ptr       | 512-byte Command Block Wrapper buffer |
| 0x1e960  | _gAccessory     | ptr       | EAAccessory reference                 |
| 0x1e980  | _buf            | ptr       | General buffer pointer                |
| 0x22a20  | mucamera_queue  | struct    | Queue for completed JPEG frames       |
| 0x22a38  | filter_num      | int32     | Filter/processing parameter           |
| 0x22a40  | callback_block  | ptr       | Callback function pointer (16 bytes)  |
| 0x22a50  | init_flag       | int32     | mfiCameraInit done flag               |
| 0x22a58  | rx_buffer       | ptr       | 2048-byte receive accumulation buffer |
| 0x22a60  | rx_buf_len      | int32     | Current bytes in rx_buffer            |
| 0x22a64  | devflag         | int32     | Device state (0=init, 1=type1, 2=type2) |
| 0x22a68  | zoomUp_count    | int32     | Zoom up button press counter          |
| 0x22a6c  | zoomDown_count  | int32     | Zoom down button press counter        |
| 0x22a70  | btn3_count      | int32     | Button 3 press counter                |
| 0x22a74  | btn4_count      | int32     | Button 4 press counter                |
| 0x22a78  | last_seq_num    | byte      | Last frame sequence number received   |
| 0x22a80  | cur_frame       | ptr       | Current mu_camera_data being assembled|
| 0x22a88  | devinfo         | [0x62]    | vs_devinfo_t - device info structure  |
| 0x22aea  | response_buf    | [0x400]   | Response buffer for cmd ack checking  |
| 0x22eec  | response_len    | int32     | Length of data in response_buf        |

---

## 8. Initialization Sequence

### 8.1 mfiCameraInit (called once)

```c
void mfiCameraInit(callback_struct *cb) {
    if (init_flag != 0) return;     // already initialized

    memcpy(&callback_block, cb, 16);  // store 16-byte callback struct
    muqueue_init(&mucamera_queue);    // init frame delivery queue
    rx_buffer = malloc(0x800);        // alloc 2048-byte receive buffer
    rx_buf_len = 0;
    init_flag = 1;
}
```

### 8.2 Full Connection Sequence

1. `openSession()` -- establish MFi/EASession with accessory
2. `mfiCameraInit(callback)` -- initialize protocol state
3. Start `_zCamera_read_thread` -- begin reading from accessory
4. Start `_data_thread` -- begin processing completed frames
5. `openCamera()`:
   a. `begincmd()` -- acquire command mutex
   b. `opentypepro`: send `[BB AA 05 00 00]`, wait for CID_GET_ALLINFO response
   c. `opencamerapro`: send `[BB AA 06 00 00]`, wait for CID_OPEN_STREAM response
   d. `endcmd()` -- release command mutex
6. Video frames begin arriving as CID 0x07/0x0A packets

---

## 9. Quick Reference: Wire Bytes

### Start Video Stream
```
Send: BB AA 05 00 00           (get device info, wait for ack)
Send: BB AA 06 00 00           (open camera stream, wait for ack)
```

### Sleep Camera
```
devflag 1: BB AA 06 00 00
devflag 2: BB AA 08 00 00
```

### Wake Camera
```
devflag 1: BB AA 05 00 00
devflag 2: BB AA 06 00 00
```

### Switch Camera
```
Send: BB AA 0B 03 00 [cam_id:1] [resolution:LE16]
```

### Response Packet (Generic)
```
AA BB [cid:1] [payload_len:LE16] [payload:N]
  -- or --
BB AA [cid:1] [payload_len:LE16] [payload:N]
```

---

## 10. devflag State Machine

The `devflag` variable tracks device protocol negotiation state:

```
devflag = 0  (initial, unknown)
    |
    v  CID 0x07 response received
devflag = 1  (device type 1 / older protocol)
    |
    v  CID 0x05 response with full devinfo
devflag = 2  (device type 2 / full-featured protocol)
```

The devflag determines which sleep/wake command variant to use, and whether
`getdevinfo()` can succeed (requires devflag == 2).

---

## 11. Observed Device Behavior (TD300 / NTC100 fw 1.0.41)

### 11.1 DevInfo Response (CID 0x05)

Full response is 512 bytes (507 payload after 5-byte header). Structure with 2-byte
prefix before the vs_devinfo_t fields:

```
Offset  Size   Field              Observed Value
------  -----  -----------------  --------------------------------
0x00    2      prefix             00 01
0x02    16     fw_info.vendor     "TESLONG"
0x12    16     fw_info.product    "NTC100"
0x22    16     fw_info.version    "Ver.1.0.41"
0x32    1      unknown            01
0x33    3      device_id          13 60 b3
0x36    5      padding            00 00 00 00 00
0x3B    15     serial_number      5513514793029732104153325a3337
0x4A    1      separator          00
0x4B    1      cam_num            01 (1 camera)
0x4C    1      cam_cur            00 (camera index 0)
0x4D    1      res_cur            08 (resolution index 8)
0x4E    2      res_list           00 08 (one entry: index 8)
0x50    ...    padding            all zeros to end
```

### 11.2 Resolution Limitations

- Resolution index 8 = 1280x720 MJPEG
- CID 0x0B (changeCamera) tested with values: 0x01-0x04, 0x09, 0x0A, 0x0C, 0x0F, 0x10, 0x12
- ALL returned `BB AA 0B 00 00` with trailing status byte `05` (error)
- The `_check_cmd_return` function checks `buf[5] == 0` for success; status 5 = rejected
- res_list contains only one entry (8), confirming this firmware only supports 720p video
- The "1920x1080" in TD300 product spec likely refers to sensor resolution or photo mode

### 11.3 Video Stream Characteristics

- Frame size: ~55-60KB per JPEG frame at 1280x720
- Frame rate: ~10 fps
- First video chunk starts with `AA BB 0A FB 01` (CID 0x0A, large payload)
- JPEG data begins at offset 12 within each packet (5 outer + 7 inner header)
- Typical frame spans multiple USB bulk reads (~44KB per complete frame)
- JPEG boundaries: FFD8 start marker, FFD9 end marker (trailing zeros may follow)

---

## 12. Notes and Caveats

- The sync word can appear as either `BB AA` or `AA BB`. Both are accepted
  by the parser. Commands sent by the host use `BB AA`.
- The `_g512CBW` buffer name suggests USB Bulk-Only Transport "Command Block
  Wrapper" heritage, but the actual MFi protocol sends 5-byte packets, not
  512-byte CBW blocks. The 512-byte buffer may be used for USB mass storage
  emulation or as a legacy naming artifact.
- The `_check_cmd_return` function reads from a separate response buffer at
  `0x22aea`, not from the main rx_buffer. Responses are copied there by
  `_handle_pro` when CID 0x06 or 0x0B is received.
- Maximum frame size enforced by `mu_camera_data_add` uses the value from
  `0x1e848` as the limit parameter.
- The protocol supports multiple cameras (`cam_info.cam_num`) and resolutions
  (`cam_info.res_list[]`), switched via the CID 0x0B command.
- Button press events (photo capture, zoom) are embedded in video frame
  headers, not sent as separate command responses.
