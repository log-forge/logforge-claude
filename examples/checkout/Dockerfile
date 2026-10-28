FROM node:22.22.3-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/examples/checkout
COPY sdk/javascript/index.js /app/sdk/javascript/index.js
COPY sdk/javascript/package.json /app/sdk/javascript/package.json
COPY sdk/python/logforge_out.py /app/sdk/python/logforge_out.py
COPY examples/checkout/backend.mjs examples/checkout/database.py examples/checkout/frontend.js examples/checkout/index.html ./
ENV HOST=0.0.0.0 PORT=38500 LOGFORGE_PYTHON=python3 PYTHONPATH=/app/sdk/python PYTHONUNBUFFERED=1
USER node
EXPOSE 38500
HEALTHCHECK --interval=2s --timeout=2s --start-period=5s --retries=20 CMD node -e "fetch('http://127.0.0.1:38500/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "backend.mjs"]
