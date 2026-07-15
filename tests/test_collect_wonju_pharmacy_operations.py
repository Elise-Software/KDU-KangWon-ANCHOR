import importlib.util
import sys
from pathlib import Path

from bs4 import BeautifulSoup


MODULE = Path(__file__).parents[1] / "scripts" / "collect_wonju_pharmacy_operations.py"
spec = importlib.util.spec_from_file_location("pharmacy_operations", MODULE)
pharmacy_operations = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pharmacy_operations
spec.loader.exec_module(pharmacy_operations)


def test_extract_table_cells_repairs_commented_closing_cells():
    row = BeautifulSoup(
        """
        <tr>
          <td>First Pharmacy *Public Late-Night</td><td>1 Test-ro</td><td>033-000-0000</td>
          <td>10:00~01:00 <!--</td--><td>10:00~01:00 <!--</td--><td>10:00~01:00 <!--</td-->
        </tr>
        """,
        "html.parser",
    ).find("tr")

    assert pharmacy_operations.extract_table_cells(row) == [
        "First Pharmacy *Public Late-Night",
        "1 Test-ro",
        "033-000-0000",
        "10:00~01:00",
        "10:00~01:00",
        "10:00~01:00",
    ]


def test_normalize_hours_aligns_hour_only_and_colon_notation():
    assert pharmacy_operations.normalize_hours("10시 ~ 1시") == "10:00~1:00"
    assert pharmacy_operations.normalize_hours("10:00 ~ 01:00") == "10:00~01:00"
