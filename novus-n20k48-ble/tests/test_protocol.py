"""
Protocol-layer tests. Pure functions, no hardware, no BLE.
Run with:  pytest tests/test_protocol.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import novus_protocol as p


def test_crc16_modbus_check_value():
    # The standard CRC-16/MODBUS check value for ASCII "123456789" is 0x4B37.
    assert p.crc16_modbus(b"123456789") == 0x4B37


def test_build_read_is_wellformed_and_parses():
    frame = p.build_read_registers(p.REG_SETPOINT, 8)
    assert frame.startswith(p.MAGIC)              # 0x5A magic header
    parsed = p.parse_frame(frame)                 # round-trips through the parser
    assert parsed is not None and parsed.crc_ok, "built frame must parse with valid CRC"


def test_corrupt_frame_rejected():
    frame = bytearray(p.build_read_registers(p.REG_PV, 4))
    frame[-1] ^= 0xFF                              # break the CRC
    parsed = p.parse_frame(bytes(frame))
    assert parsed is None or not parsed.crc_ok


def test_frame_assembler_reassembles_split_stream():
    frame = p.build_read_registers(p.REG_SETPOINT, 4)
    fa = p.FrameAssembler()
    out = fa.feed(frame[:3]) + fa.feed(frame[3:])  # arrives in two BLE chunks
    assert len(out) == 1


def test_program_base_layout():
    assert p.program_base(1) == p.RS_PROG_BASE          # 400
    assert p.program_base(2) == 400 + p.RS_PROG_STRIDE  # 440
    assert p.program_base(20) == 400 + 19 * p.RS_PROG_STRIDE
    assert p.RS_SEGMENTS == 9


def test_program_select_register_is_247():
    # The key correction from the reverse-engineering: 247 selects the program.
    assert p.REG_RS_PRN_EXEC == 247
    assert p.REG_CTRL_RUN == 214 and p.REG_CTRL_AUTO == 213


def test_function_codes():
    assert p.FC_READ_REGISTERS == 0x46
    assert p.FC_WRITE_SINGLE == 0x47
    assert p.FC_WRITE_MULTIPLE == 0x48
