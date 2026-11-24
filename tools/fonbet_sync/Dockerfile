FROM mcr.microsoft.com/playwright:v1.62.1-noble
RUN apt-get update && apt-get install -y --no-install-recommends x11vnc novnc websockify && rm -rf /var/lib/apt/lists/*
WORKDIR /work
ENV DISPLAY=:99
CMD ["bash", "/work/start-browser.sh"]
