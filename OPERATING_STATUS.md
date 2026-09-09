# Operating status

Scheduled trading is paused. The corrected cached-data replay returned a 1.091 profit factor, 1.80% CAGR, and 12.09% maximum drawdown across 235 trades. Early-sample profit factor was 0.989 and late-sample profit factor was 1.240, so the evidence is not stable enough for unattended scheduling.

This strategy also trades QQQ in the same Alpaca paper account as the 15-minute ORB, CVD, and Orochi bots. Those processes cannot safely own and close separate QQQ positions in one netted account. Manual dispatch remains available for isolated testing.
