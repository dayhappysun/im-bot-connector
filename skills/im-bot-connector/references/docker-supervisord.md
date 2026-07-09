# Hermes Dockerfile — add supervisord for connector management

# 1. Install supervisord
RUN apt-get update && apt-get install -y supervisor && rm -rf /var/lib/apt/lists/*

# 2. Add im-bot connector listener config
COPY supervisor-imbot.conf /etc/supervisor/conf.d/hermes-imbot.conf

# 3. Start supervisord instead of running hermes directly
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/supervisord.conf"]
