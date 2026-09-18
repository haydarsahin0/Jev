"""Futbol skor tahmini: gecmis maclardan egitim + Jev'in tipli karari.

Alt moduller:
    data      - mac verisi saglayicilari ve kanonik kayit bicimi
    ratings   - zaman agirlikli Dixon-Coles Poisson MLE ve Elo
    grid      - skor izgarasi, 1X2/AU/KG turetme ve ters cozum
    features  - fikstur basina sizintisiz ozellik demeti
    jev       - tipli soru seti, state uretimi, yanit normalizasyonu
    blend     - istatistik tabani ile Jev'in harmanlanmasi
    backtest  - ileri yuruyen degerlendirme ve kalibrasyon
    cli       - fetch / train / backtest / predict komutlari
"""

__all__ = [
    "data",
    "ratings",
    "grid",
    "features",
    "jev",
    "blend",
    "backtest",
    "cli",
]
