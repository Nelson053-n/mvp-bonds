# Ops

Versioned operational config that lives on the prod server (`192.168.10.114`) but
is **not** auto-deployed by `git pull`. Apply changes here to the server manually.

## nginx (`nginx-bondai.conf`)

Mirror of `/etc/nginx/sites-enabled/mvp-bonds`. The app listens on `127.0.0.1:8000`;
nginx terminates TLS for `bondai.ru` and reverse-proxies to it.

### www.bondai.ru → bondai.ru redirect (pending DNS)

`www.bondai.ru` currently **does not resolve** — there is no DNS A-record for it.
Until that exists, the redirect cannot work and the cert cannot cover www. Rollout:

1. **DNS** (registrar / DNS panel): add `A  www  → <server IP>` (same IP as the apex
   `bondai.ru` record). Wait for propagation (`dig +short www.bondai.ru`).
2. **TLS**: extend the Let's Encrypt cert to include www:
   ```
   certbot --nginx -d bondai.ru -d www.bondai.ru
   ```
   (or `certbot certonly --webroot -w /var/www/certbot -d bondai.ru -d www.bondai.ru`
   then reload). Verify: `echo | openssl s_client -servername www.bondai.ru -connect bondai.ru:443 2>/dev/null | openssl x509 -noout -text | grep -A1 'Subject Alternative Name'`
3. **nginx**: copy this file to `/etc/nginx/sites-enabled/mvp-bonds`, then:
   ```
   nginx -t && systemctl reload nginx
   ```
4. **Verify**: `curl -sI https://www.bondai.ru/ | grep -i location` → `https://bondai.ru/`

Until step 1 is done, leave the live server config as-is (apex-only). The HTTPS
`www` server block in this file is harmless once the cert covers www, but will make
`nginx -t` succeed yet TLS handshakes to www fail if the cert is apex-only — so do
not enable it before step 2.
