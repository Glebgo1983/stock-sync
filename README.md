# insales-mpfit-stock-sync

Интеграция inSales ↔ mpFit: заказы (создание заказа в mpFit при новом заказе inSales,
проставление статуса доставки обратно в inSales) + синхронизация остатков (актуализация
`quantity` у вариантов inSales по данным склада mpFit, чтобы товары без остатка
автоматически помечались как "нет в наличии").

Основан на существующей у заказчика интеграции заказов — логика заказов
(`api/functions.py`, `api/cdek_functions.py`, `api/pochta_functions.py`) не менялась.

## Переменные окружения

Смотрите `.env.example`. Локально создайте `.env.local` (в git не попадает).
На Vercel заведите те же переменные в Project Settings -> Environment Variables.

- `MPFIT_TOKEN` — Bearer-токен mpFit. Получить можно только через их Telegram-поддержку
  (см. `help.mpfit.ru`), self-service выдачи ключа нет.
- `INSALES_USERNAME` / `INSALES_PASSWORD` — ключ приложения inSales (Basic Auth).
- `INSALES_SHOP_DOMAIN` — домен магазина, например `myshop-dbn10.myinsales.ru`.
- `CRON_SECRET` — секрет для эндпоинта `/api/sync-stock`. Важно использовать именно
  это имя переменной: Vercel Cron Jobs сам подставляет её в заголовок
  `Authorization: Bearer $CRON_SECRET` при вызове по расписанию, без доп. настройки.

## Синхронизация остатков

`GET /api/sync-stock` — тянет полный каталог остатков mpFit (`/v1/products/stocks`) и
список товаров inSales (`/admin/products.json`), сопоставляет их по артикулу
(`article` в mpFit == `sku` варианта в inSales) и одним batched-запросом
(`PUT /admin/products/variants_group_update.json`) обновляет `quantity` у совпавших
вариантов.

Товары inSales, чей sku не найден в ответе mpFit вообще (например, цифровые товары
или то, что не ведётся через склад mpFit), **не трогаются** — остаются с текущим
остатком. Список таких sku возвращается в ответе (`unmatched_skus`) для ручной проверки.

**Важно:** чтобы inSales сам скрывал товар при `quantity=0`, у товара в самом
магазине должен быть включён учёт остатка ("Ограничить продажу количеством товара
на складе" в настройках товара/магазина). Это настройка на стороне inSales, вне
зоны кода этой интеграции — стоит проверить у заказчика перед первым боевым запуском.

### Проверка перед боевым запуском

```
curl -H "Authorization: Bearer $CRON_SECRET" \
  "https://<ваш-домен-на-vercel>/api/sync-stock?dry_run=true"
```

`dry_run=true` считает и возвращает сводку (`matched_variants`, `unmatched_count`,
`unmatched_skus`, ...), но ничего не пишет в inSales. Проверьте цифры, прежде чем
включать `dry_run=false` (обычный вызов без параметра тоже пишет в inSales).

## Расписание запуска

`vercel.json` уже содержит cron на каждые 15 минут:

```json
{ "path": "/api/sync-stock", "schedule": "*/15 * * * *" }
```

Ограничение: на бесплатном (Hobby) плане Vercel Cron Jobs может запускаться не чаще
одного раза в сутки — если план Hobby, либо переходите на Pro, либо настройте внешний
планировщик (например, cron-job.org) на вызов того же URL с тем же заголовком
`Authorization: Bearer <CRON_SECRET>` с нужной периодичностью.

## Локальный запуск

```
pip install -r requirements-dev.txt
cp .env.example .env.local   # заполнить реальными значениями
uvicorn api.index:app --reload
```

## Тесты

```
pip install -r requirements-dev.txt
pytest
```

Тесты покрывают чистую логику (подсчёт доступного остатка, включая смарт-товары, и
сопоставление sku между системами) на моковых данных — без обращения к реальным API.
