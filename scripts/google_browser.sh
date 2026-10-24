#!/bin/sh
set -eu

Xvfb :99 -screen 0 1440x1000x24 -ac +extension GLX +render -noreset >/tmp/xvfb.log 2>&1 &
export DISPLAY=:99
until xdpyinfo -display :99 >/dev/null 2>&1; do sleep 0.2; done

# Docker may recreate this service with a new container hostname. Chromium's
# singleton files can then falsely report that the persistent profile is open.
rm -f /data/google-flights-profile/SingletonLock \
  /data/google-flights-profile/SingletonCookie \
  /data/google-flights-profile/SingletonSocket

x11vnc -display :99 -forever -shared -nopw -listen 0.0.0.0 -rfbport 5900 >/tmp/x11vnc.log 2>&1 &
websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/novnc.log 2>&1 &
socat TCP-LISTEN:9222,bind=0.0.0.0,reuseaddr,fork TCP:127.0.0.1:9223 >/tmp/cdp-proxy.log 2>&1 &
chromium --no-sandbox --disable-dev-shm-usage --disable-gpu --remote-debugging-address=127.0.0.1 --remote-debugging-port=9223 --remote-allow-origins='*' --user-data-dir=/data/google-flights-profile --lang=en-GB --window-size=1440,1000 'https://www.google.com/travel/flights?hl=en&curr=EUR' >/tmp/chromium.log 2>&1 &

wait
