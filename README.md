# CameraDVR

Transforma a tela de um PC Windows em uma **câmera IP** que qualquer DVR/NVR com suporte a
**ONVIF** ou **RTSP** (Intelbras, Hikvision, Dahua, etc.) consegue adicionar e gravar.
Também registra os **cliques do mouse** e qual programa/janela estava em uso. O sistema é
vendido por **assinatura**: só funciona enquanto o plano estiver pago.

```
 PC do funcionário (agente)                         Seu servidor (nuvem)
┌────────────────────────────────┐   HTTPS       ┌──────────────────────────────┐
│ ffmpeg  → captura a tela        │ ───────────▶  │ API de licenças (FastAPI)    │
│ mediamtx→ RTSP :8554/tela       │  ativação,    │  planos / clientes / chaves  │
│ ONVIF   → :8000 + descoberta    │  heartbeat,   │  limite de PCs por plano     │
│ cliques → fila → envia em lote  │  cliques      │  Mercado Pago (recorrente)   │
│ ícone na bandeja + aviso        │               │  painel da empresa /painel   │
└───────────────┬────────────────┘               └──────────────────────────────┘
                │ RTSP (rede local)
          ┌─────▼─────┐
          │  DVR/NVR  │  grava como se fosse uma câmera comum
          └───────────┘
```

## Como funciona o controle de acesso (se pagou, usa)

1. Cada **assinatura** tem uma **chave de licença** (`CDVR-XXXXX-XXXXX-XXXXX-XXXXX`) e um **plano**
   que define: nº máximo de PCs, FPS, resolução, se tem registro de cliques e por quantos dias guardar.
2. O agente ativa o PC com a chave. O servidor só aceita se a assinatura estiver **paga** e houver
   **vaga** no plano.
3. O servidor devolve um **token assinado (Ed25519)** com validade de 72 h (configurável). O agente
   verifica a assinatura com a chave pública embutida, ou seja, não dá para falsificar nem editar.
4. A cada 5 min o agente faz *heartbeat* e renova o token. Se a assinatura vencer ou for
   cancelada, o servidor responde **402** e o agente **para a transmissão na hora**, sumindo
   também da busca ONVIF do DVR.
5. Sem internet, o agente continua até o token expirar (tolerância offline). Voltar o relógio do
   Windows para trás é detectado e bloqueia o agente.
6. Depois do vencimento há uma **tolerância de 3 dias** (`GRACE_DAYS`). Num cancelamento, o
   cliente usa até o fim do período que já pagou.

Planos criados automaticamente (edite em `server/app/main.py` ou pela API):

| Plano        | Preço/mês | PCs | FPS | Resolução | Retenção dos cliques |
|--------------|-----------|-----|-----|-----------|----------------------|
| Básico       | R$ 49,90  | 3   | 5   | 720p      | 15 dias              |
| Profissional | R$ 129,90 | 10  | 10  | 1080p     | 60 dias              |
| Empresa      | R$ 349,90 | 50  | 15  | 1080p     | 180 dias             |

## Subindo o servidor

```bash
cd server
pip install -r requirements.txt
python scripts/gen_keys.py ../keys          # gera keys/license_private.pem e license_public.pem
cp ../keys/license_public.pem ../agent/cameradvr_agent/license_public.pem
export LICENSE_PRIVATE_KEY_FILE=../keys/license_private.pem
export ADMIN_API_KEY='uma-chave-grande-e-secreta'
export MP_ACCESS_TOKEN='APP_USR-...'         # Mercado Pago
export MP_WEBHOOK_SECRET='...'               # "Assinatura secreta" das notificações do MP
export PUBLIC_BASE_URL='https://licencas.seudominio.com.br'
uvicorn app.main:app --port 8080
```

> A chave **privada** fica só no servidor. Se ela vazar, qualquer pessoa pode gerar licenças.

Em produção use PostgreSQL (`DATABASE_URL=postgresql+psycopg://...`) e HTTPS. Também há um `Dockerfile`.

No Mercado Pago, configure o webhook para `https://SEU_DOMINIO/api/billing/webhook/mercadopago`
com os eventos **Planos e assinaturas** (`subscription_preapproval`, `subscription_authorized_payment`).

### Vendendo (fluxo automático)

```bash
# Seu site chama isto quando o cliente escolhe um plano:
curl -X POST https://SEU_DOMINIO/api/billing/checkout -H 'Content-Type: application/json' \
  -d '{"plan_code":"profissional","name":"Empresa X","email":"ti@empresax.com.br"}'
# -> { "license_key": "CDVR-...", "checkout_url": "https://www.mercadopago.com.br/..." }
```

O cliente paga no link do Mercado Pago. O webhook libera a chave, e cada renovação mensal estende o prazo.
Se a cobrança falhar ou o cliente cancelar, o acesso é cortado no vencimento.

### Venda manual (PIX/boleto fora do gateway)

```bash
H='X-Admin-Key: uma-chave-grande-e-secreta'
curl -X POST localhost:8080/api/admin/customers -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"Empresa X","email":"ti@empresax.com.br","document":"00.000.000/0001-00"}'
curl -X POST localhost:8080/api/admin/subscriptions -H "$H" -H 'Content-Type: application/json' \
  -d '{"customer_id":1,"plan_code":"basico"}'
curl -X POST localhost:8080/api/admin/subscriptions/1/payments -H "$H" -H 'Content-Type: application/json' -d '{}'
```

Outros endpoints de admin: `GET /api/admin/{plans,customers,subscriptions}`, `POST /api/admin/plans`,
`POST /api/admin/subscriptions/{id}/cancel[?immediate=true]`, `POST /api/admin/devices/{id}/deactivate`
(libera a vaga de um PC trocado). A documentação interativa fica em `/docs`.

### Painel da empresa cliente

`https://SEU_DOMINIO/painel`: o cliente entra com o `api_token` dele e vê:
- situação da assinatura, validade, PCs em uso e cliques do dia;
- **Renovar assinatura**: gera um pagamento avulso de 1 período (PIX, cartão ou boleto) no Mercado Pago.
  Quando o pagamento é aprovado, o webhook (`payment`) estende a validade;
- **Parar assinatura agora**: pede que o cliente digite `CANCELAR`, bloqueia os PCs em até 5 minutos
  e cancela a cobrança recorrente no Mercado Pago;
- PCs online/offline com o endereço RTSP, programas mais usados e registro de atividade com filtros;
- **Aceites e ciências**: a prova de quem aceitou os termos e de quais colaboradores leram o aviso.

## Proteção jurídica (aceite dos termos)

- **Termos de Uso** (`server/app/legal/termos_de_uso.md`): o cliente é o *controlador* (LGPD) e
  o único responsável pelo uso. Ele se compromete a informar os funcionários e a usar o sistema só em
  computadores da empresa, fica proibido o monitoramento oculto, e há cláusula de indenização e
  limitação de responsabilidade. **É um modelo: passe por um advogado.**
- **Aceite obrigatório:** sem aceitar a versão atual dos termos, o PC não ativa (HTTP 428). O
  instalador mostra o texto, exige rolar até o fim e pede nome, CPF (validado) e cargo do responsável.
- **Prova:** cada aceite fica gravado com nome, CPF, cargo, data/hora, IP, computador e o hash
  SHA-256 do texto exato aceito. Se você mudar o texto, o hash muda e é preciso aceitar de novo.
- **Ciência do colaborador:** no primeiro login de cada usuário do Windows aparece o aviso
  (`aviso_colaborador.md`). O clique em "Li e estou ciente" também é registrado no servidor.
- **Não existe modo oculto:** o ícone na bandeja e o aviso não podem ser desligados pela configuração.

## Telas do agente (PC)

O agente usa **pywebview**, que no Windows roda no Edge WebView2 (já vem no Windows 10/11):
- `CameraDVR.exe --instalar`: assistente (chave → Termos de Uso → dados para o DVR);
- `CameraDVR.exe --status`: janela de status, também aberta pelo ícone da bandeja;
- `CameraDVR.exe --aviso`: aviso ao colaborador.

Para implantar em muitos PCs sem interface:
`CameraDVR.exe --servidor URL --ativar CHAVE --nome "Fulano de Tal" --cpf 00000000000 --cargo Diretor --aceito-termos`

## Agente (PC monitorado)

Gerar o instalador, numa máquina Windows:

```powershell
cd agent
powershell -ExecutionPolicy Bypass -File build\build.ps1
```

Instalar em cada PC (como Administrador):

```powershell
cd dist\CameraDVR
powershell -ExecutionPolicy Bypass -File instalar.ps1 -Servidor https://licencas.seudominio.com.br
```

O instalador libera as portas no firewall e abre o assistente (chave, aceite dos termos e ativação).
Depois cria uma tarefa agendada que inicia o agente no logon de qualquer usuário. O assistente mostra os dados para o DVR:

```
RTSP : rtsp://192.168.0.23:8554/tela
ONVIF: http://192.168.0.23:8000/onvif/device_service
Usuário: admin
Senha  : (gerada automaticamente; fica em C:\ProgramData\CameraDVR\config.json)
```

### Adicionando no DVR/NVR

- **Busca automática (ONVIF):** no DVR, vá em *Câmeras IP → Buscar*. O PC aparece como
  `CameraDVR ScreenCam NOME-DO-PC`. Adicione com o usuário e a senha acima.
- **Manual:** protocolo ONVIF, IP do PC, porta 8000. Se o DVR não tiver ONVIF, use
  "RTSP personalizado" com a URL `rtsp://IP:8554/tela`.
- Recomendado: IP fixo (ou reserva DHCP) para cada PC.

## Aspectos legais (LGPD / CLT): leia antes de vender

Monitorar o computador **corporativo** é permitido no Brasil, desde que com **transparência e
finalidade legítima**. Para proteger você e seus clientes, o produto já vem assim:

- **Aviso visível e obrigatório:** um ícone na bandeja e um aviso de ciência no primeiro login de cada usuário.
  Não há modo oculto: monitoramento secreto aumenta muito o risco de ações trabalhistas e de sanções da ANPD.
- **Sem keylogger:** só registra cliques, programa e título da janela. Não grava teclas
  digitadas nem senhas.
- **Retenção limitada:** os eventos são apagados automaticamente após o prazo do plano.
- Inclua no contrato com o cliente que **ele** (o empregador) é o *controlador* dos dados e deve
  ter uma política de uso dos equipamentos assinada pelos funcionários. Você atua como *operador*.
  Vale a revisão de um advogado.

## Testes

```bash
cd server && python -m pytest -q
cd agent  && python -m pytest -q
```

## Próximos passos sugeridos

- Site de vendas que chame `/api/billing/checkout`.
- Assinatura digital do `.exe` (Authenticode), para o antivírus/SmartScreen não bloquear.
- Atualização automática do agente.
- Vários monitores (hoje captura a área de trabalho inteira como uma imagem só).
- Relatórios no painel (tempo por programa, ociosidade).
