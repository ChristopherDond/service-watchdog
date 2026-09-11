<p align="center">
  <img src="https://img.shields.io/github/license/ChristopherDond/service-watchdog" alt="Licença" />
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python" />
  <img src="https://img.shields.io/badge/depend%C3%AAncias-zero-brightgreen" alt="Dependências" />
</p>

<div align="right">

[English](README.md)

</div>

# service-watchdog

**service-watchdog** mantém serviços locais vivos: testa endpoints HTTP/TCP em intervalos, reinicia o serviço caído após N falhas consecutivas, registra cada incidente em `incidents.jsonl` e alerta no Telegram na hora em que algo cai. Feito pra homelab, dashboards self-hosted, servidores de jogo e gateways de IA locais — qualquer coisa que quebra quando ninguém está olhando.

## O que faz

- Testa cada serviço do `config.json` (`http` GET ou conexão `tcp` pura).
- Conta falhas consecutivas por serviço.
- Roda o `restart_command` após `failure_threshold` falhas.
- Respeita o `cooldown` por serviço para não reiniciar em loop.
- Anexa cada restart ao `incidents.jsonl`.
- Envia alerta Telegram `sendMessage` quando habilitado.

## Como funciona

Probe:

- `probe=http` → `GET {url}{health_path}` com timeout. Saudável = status 2xx–3xx.
- `probe=tcp` → `socket.create_connection(host, porta)` com timeout. Saudável = conectou. Útil para bancos de dados, servidores de jogo ou qualquer serviço TCP que não fala HTTP.

Restart:

- Falhas rastreadas por serviço em `.watchdog_state.json` (`failures`, `last_restart`).
- Sucesso zera o contador.
- Quando `failures >= failure_threshold` e `cooldown` expirou → `subprocess.run(restart_command, shell=True)`, zera contador, atualiza `last_restart`.

Incidentes:

- Um objeto JSON por linha em `incidents.jsonl`: `timestamp`, `service`, `url`, `failures`, `restart_command`, `returncode`, `restarted`.

## Instalação

```bash
cd service-watchdog
pip install -r requirements.txt
cp config.json.example config.json
```

Python 3.11+, só stdlib. O `requirements.txt` tem só o `pytest` para os testes.

## Configuração

Copie `config.json.example` para `config.json` e edite. Nunca commite `config.json` com tokens reais (está no gitignore).

| Campo | Nível | Descrição |
|---|---|---|
| `check_interval` | raiz | Segundos entre probes no modo `daemon` |
| `failure_threshold` | raiz | Falhas seguidas padrão antes do restart |
| `incidents_file` | raiz | Caminho do `incidents.jsonl` |
| `telegram.enabled` | raiz | `true` para enviar alertas Telegram |
| `telegram.bot_token` | raiz | Token do BotFather (só no `config.json`, nunca commitar) |
| `telegram.chat_id` | raiz | Id do chat/canal destino |
| `telegram.parse_mode` | raiz | `HTML` (padrão) |
| `name` | serviço | Nome único do serviço |
| `url` | serviço | URL base (`http://host:porta`) ou `host:porta` para TCP |
| `health_path` | serviço | Anexado ao `url` nos probes HTTP (ex. `/health`) |
| `probe` | serviço | `http` ou `tcp` |
| `timeout` | serviço | Timeout do probe em segundos |
| `restart_command` | serviço | Comando shell no erro (ex. `docker restart x`) |
| `cooldown` | serviço | Segundos mínimos entre restarts do serviço |
| `failure_threshold` | serviço | Opcional, sobrescreve o padrão por serviço |

## Uso

```bash
python watchdog.py check --config config.json.example --dry-run
python watchdog.py check --config config.json
python watchdog.py run --config config.json --state .watchdog_state.json
python watchdog.py daemon --config config.json --interval 30
python watchdog.py daemon --config config.json --iterations 5 --interval 1
```

Subcomandos:

- `check` — testa uma vez, imprime `OK`/`FAIL`, nunca reinicia. Exit 1 se algum serviço falhar.
- `run` — testa uma vez, reinicia serviços acima do limite, salva estado, registra incidentes.
- `daemon` — repete o `run` para sempre (`--iterations N` limita voltas, bom p/ cron/teste). `Ctrl+C` sai limpo.

## Tests

```bash
python -m pytest
```
