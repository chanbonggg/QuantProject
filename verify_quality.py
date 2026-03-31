#!/usr/bin/env python3
"""
Quick verification script for quality.py implementation
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    print("1. 모듈 import 확인...")
    from quant_us.strategies.quality import (
        compute_signal,
        get_portfolio,
        _calculate_quality_metrics,
        _winsorize_value,
        _zscore_normalize,
        _get_all_recent_financials,
    )
    print("   ✓ quality.py 모든 함수 import 성공")

    print("\n2. 함수 서명 확인...")
    import inspect

    # compute_signal
    sig = inspect.signature(compute_signal)
    assert "date" in sig.parameters
    assert "universe" in sig.parameters
    assert "conn" in sig.parameters
    print("   ✓ compute_signal(date, universe, conn) 서명 OK")

    # get_portfolio
    sig = inspect.signature(get_portfolio)
    assert "date" in sig.parameters
    assert "conn" in sig.parameters
    print("   ✓ get_portfolio(date, conn) 서명 OK")

    # _calculate_quality_metrics
    sig = inspect.signature(_calculate_quality_metrics)
    assert "ticker" in sig.parameters
    assert "date" in sig.parameters
    assert "conn" in sig.parameters
    print("   ✓ _calculate_quality_metrics(ticker, date, conn) 서명 OK")

    # _winsorize_value
    sig = inspect.signature(_winsorize_value)
    assert "value" in sig.parameters
    print("   ✓ _winsorize_value(value, ...) 서명 OK")

    # _zscore_normalize
    sig = inspect.signature(_zscore_normalize)
    assert "series" in sig.parameters
    print("   ✓ _zscore_normalize(series) 서명 OK")

    # _get_all_recent_financials
    sig = inspect.signature(_get_all_recent_financials)
    assert "ticker" in sig.parameters
    assert "date" in sig.parameters
    assert "conn" in sig.parameters
    print("   ✓ _get_all_recent_financials(ticker, date, conn, limit) 서명 OK")

    print("\n3. 테스트 파일 import 확인...")
    from quant_us.tests.test_strategies import (
        test_quality_winsorize_normal,
        test_quality_winsorize_extreme,
        test_zscore_normalize_basic,
        test_compute_signal_basic,
        test_get_portfolio_basic,
    )
    print("   ✓ test_strategies.py 테스트 함수 import 성공")

    print("\n4. 기본 동작 검증...")

    # Winsorize 테스트
    assert _winsorize_value(1.5) == 1.5
    assert _winsorize_value(6.0) == 5.0
    assert _winsorize_value(5.0) == 5.0
    print("   ✓ 윈저라이징 함수 정상 작동")

    # Z-score 테스트
    import pandas as pd
    import numpy as np

    data = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    result = _zscore_normalize(data)
    assert abs(result.mean()) < 1e-10
    assert abs(result.std() - 1.0) < 0.01
    print("   ✓ Z-score 정규화 함수 정상 작동")

    print("\n✅ 모든 검증 통과!")
    print("\n다음 단계:")
    print("1. pytest를 사용하여 전체 테스트 실행: pytest quant_us/tests/test_strategies.py -v")
    print("2. 계획서 STEP 3-0 (universe.py)와 연결 대기")
    print("3. STEP 3-A/B 완료 후 병렬 테스트 실행")

except ImportError as e:
    print(f"❌ Import 오류: {e}")
    sys.exit(1)
except AssertionError as e:
    print(f"❌ 검증 실패: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ 예상치 못한 오류: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
