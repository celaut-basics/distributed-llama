| case | runs | pp tok/s | tg tok/s | TTFT s | cold start s | RPC load MB | CPU cores (gen) | MU spent (M) | mode |
|---|---|---|---|---|---|---|---|---|---|
| host-w0 | 3 | 162.3 | 30.8 | 2.21 | 1.6 | 0 | 3.69 | - | local |
| host-w1 | 3 | 177.4 | 29.3 | 2.03 | 2.6 | 0 | 3.64 | - | distributed |
| host-w2 | 3 | 175.1 | 28.2 | 2.05 | 2.6 | 0 | 3.73 | - | distributed |
| host-w3 | 3 | 174.0 | 26.9 | 2.07 | 2.6 | 0 | 4.08 | - | distributed |
| live-w0 | 3 | 157.6 | 29.7 | 2.29 | 32.2 | 0 | 3.85 | 0.22 | local |
| live-w1 | 3 | 163.2 | 27.9 | 2.21 | 40.6 | 982 | 3.55 | 0.52 | distributed |
| live-w2 | 3 | 154.1 | 26.1 | 2.34 | 52.4 | 982 | 4.05 | 0.84 | distributed |
| live-w3 | 3 | 151.6 | 24.9 | 2.38 | 54.2 | 982 | 4.21 | 1.17 | distributed |
| two-w1 | 3 | 171.0 | 28.4 | 2.10 | 63.8 | 982 | 3.53 | 10.64 | distributed |
| two-w2 | 3 | 167.6 | 26.8 | 2.15 | 59.7 | 982 | 4.01 | 20.91 | distributed |
| two-w3 | 3 | 162.7 | 25.6 | 2.21 | 84.9 | 982 | 4.35 | 31.65 | distributed |
