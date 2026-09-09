# ЗЕРКАЛО БОЕВОГО КОНФИГА: /etc/nginx/sites-available/bondai.ru
# Снято с прода 212.8.228.248 10.09.2026. Это РАБОЧИЙ конфиг, не черновик.
#
# НЕ ПУТАТЬ с ops/nginx-bondai.conf — тот файл ЧЕРНОВИК и накатывать его нельзя.
#
# Файл хранится в git только ради истории и восстановления: автодеплой его НЕ
# применяет, nginx читает свою копию из /etc. После правки на сервере обнови и
# этот файл, иначе они разъедутся.
#
# Накатка (вручную, с проверкой — на сервере ещё ~10 ЧУЖИХ сайтов, битый
# конфиг отвергается целиком и кладёт их все):
#   scp ops/nginx-prod-sites-bondai.ru root@212.8.228.248:/tmp/bondai.ru
#   ssh root@212.8.228.248 'cp /etc/nginx/sites-available/bondai.ru /root/bondai.ru.bak-$(date +%%F-%%H%%M%%S) \
#     && cp /tmp/bondai.ru /etc/nginx/sites-available/bondai.ru \
#     && nginx -t && systemctl reload nginx'
#
# Зависимость: зона bond_rl объявляется в ops/nginx-prod-conf.d-bondai-ratelimit.conf
# → /etc/nginx/conf.d/. Без неё nginx -t упадёт с "unknown limit_req zone".
#
# Блоки с пометкой "managed by Certbot" правит certbot при продлении —
# свои изменения туда не вносить, затрёт.

server {
    server_name bondai.ru www.bondai.ru;
    # Сканеры уязвимостей: 3201 запрос в сутки по путям WordPress/.env/.git,
    # которых у нас нет и не будет (проект — FastAPI, не PHP). Отдаём 444
    # (закрыть соединение молча) и не пишем в лог, чтобы не топить в шуме
    # реальные 404. Правило живёт ТОЛЬКО в server-блоке bondai.ru: соседние
    # сайты на этом сервере — настоящий WordPress, у них такие пути легитимны.
    #
    # Паттерн заякорен на границу сегмента пути (^/(.*/)? ... (/|$)): без якоря
    # ветка \.(env|git|...) резала бы ЛЮБОЙ путь с таким окончанием —
    # /static/app.env и /bond/TEST.env уходили в 444 молча и без лога.
    # Группа (\.php)? нужна для /wp-login.php: там после имени идёт .php,
    # а не граница сегмента, и без неё путь проскакивал мимо правила.
    location ~* ^/(.*/)?((wp-admin|wp-login|wp-includes|wp-json|wp-content|wordpress|xmlrpc|phpmyadmin)(\.php)?(/|$)|\.(env|git|aws|ssh)(/|$)) {
        access_log off;
        return 444;
    }

    # Публичные страницы бумаг: лимит частоты для краулеров (зона в
    # conf.d/bondai-ratelimit.conf). Матч именно regex ^/bond(/|$), а НЕ
    # префикс "/bond": префикс захватывал бы и API-роутер /bonds/* —
    # автодополнение поиска (/bonds/suggest) получало бы 429 при быстром
    # наборе или нескольких пользователях за одним NAT.
    location ~ ^/bond(/|$) {
        limit_req zone=bond_rl burst=20 delay=5;

        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_connect_timeout 10s;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }

    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        # Дефолт nginx — 60с. Тяжёлые пути (/portfolios/*/instruments,
        # /portfolios/all/totals) упирались в него и отдавали 504.
        proxy_connect_timeout 10s;
        proxy_read_timeout 90s;
        proxy_send_timeout 90s;
    }

    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/bananagen.ru/fullchain.pem; # managed by Certbot
    ssl_certificate_key /etc/letsencrypt/live/bananagen.ru/privkey.pem; # managed by Certbot
    include /etc/letsencrypt/options-ssl-nginx.conf; # managed by Certbot
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem; # managed by Certbot










}
server {
    if ($host = www.bondai.ru) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    if ($host = bondai.ru) {
        return 301 https://$host$request_uri;
    } # managed by Certbot


    listen 80;
    server_name bondai.ru www.bondai.ru;
    return 404; # managed by Certbot




}