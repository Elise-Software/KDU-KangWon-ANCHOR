import importlib.util
from pathlib import Path

MODULE = Path(__file__).parents[1] / "scripts" / "integrate_wonju_public_data.py"
spec = importlib.util.spec_from_file_location("integrate", MODULE)
integrate = importlib.util.module_from_spec(spec); spec.loader.exec_module(integrate)

def test_normalizers():
    assert integrate.normalize_name(" 연세대학교 원주세브란스(기독)병원 ") == "연세대학교원주세브란스기독병원"
    assert integrate.normalize_address("강원도 원주시  A로 1, 2층 (중앙동)") == "강원특별자치도 원주시 A로 1"
    assert integrate.normalize_phone("033 123 4567") == "033-123-4567"

def test_exact_phone_match():
    row = {"institution_id":"wonju:1", "source_id":"1", "name":"가", "normalized_name":"가", "address":"강원특별자치도 원주시 가로 1", "phone":"033-123-4567"}
    incoming = {"name":"다른 이름", "address":"", "phone":"0331234567", "source_id":""}
    found, method, _, _ = integrate.match_institution(incoming, [row], [])
    assert found == row and method == "phone_exact"

def test_coordinate_policy():
    inst = {"institution_id":"wonju:1", "address":"강원특별자치도 원주시 가로 1"}
    row = {"address":"강원도 원주시 가로 1", "latitude":"37.2", "longitude":"127.9"}
    assert integrate.create_coordinate_record(inst, row, "x.csv", "2022-08-13")["coordinate_status"] == "source_provided"
