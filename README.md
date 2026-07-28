# Postador na nuvem (sem depender do PC ligado)

Esse repositório roda o `agendador_publicacoes.py` automaticamente a cada 15 minutos,
nos servidores do GitHub — não precisa do seu PC ligado.

## Passo a passo pra deixar funcionando (só precisa fazer uma vez)

### 1. Criar o repositório no GitHub

- Vá em github.com → **New repository**
- Nome sugerido: `postador-conteudo` (ou o que preferir)
- **Público** (decidido com você — minutos de Actions ilimitados de graça)
- NÃO marque "Add README" nem ".gitignore" (já tem tudo aqui)

Depois de criado, na sua máquina, dentro desta pasta (`_nuvem_repo`):

```bash
git init
git add .
git commit -m "primeira versao do postador na nuvem"
git branch -M main
git remote add origin https://github.com/SEU_USUARIO/postador-conteudo.git
git push -u origin main
```

### 2. Adicionar os Secrets (credenciais)

**Só Instagram + Supabase** (decidido em 28/07/2026) — YouTube e Facebook agendam nativo com UMA
chamada de API só, então continuam rodando LOCAL (no seu PC, quando você cria um lote novo), não
precisam ficar "esperando" na nuvem. Isso significa que os tokens de YouTube/Facebook nem chegam
a sair do seu PC — só 4 secrets, não 10.

No repositório no GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
Criar um secret pra cada linha abaixo. O **nome do secret** é a primeira coluna (copiar exatamente
igual, maiúsculas incluídas); o **valor** é o conteúdo INTEIRO do arquivo local indicado.

| Nome do Secret | Conteúdo (copiar o arquivo inteiro) |
|---|---|
| `INSTAGRAM_COFRINHODEFE` | `C:\Users\Jaqueline\.claude\tokens\instagram_cofrinhodefe.txt` |
| `INSTAGRAM_SACRARIUM` | `C:\Users\Jaqueline\.claude\tokens\instagram_sacrarium.txt` |
| `INSTAGRAM_FORJANDOTITAS` | `C:\Users\Jaqueline\.claude\tokens\instagram_forjandotitas.txt` |
| `SUPABASE_TXT` | `C:\Users\Jaqueline\.claude\tokens\supabase.txt` |

Esses secrets NUNCA aparecem no código nem nos logs — o GitHub já esconde automaticamente
qualquer texto que bata com um secret cadastrado.

**YouTube e Facebook continuam 100% locais**, e o Instagram fica só por conta da nuvem — pra
nunca correr risco de postar a mesma coisa duas vezes (uma local, outra na nuvem), use sempre o
flag certo em cada lugar:

- **No seu PC**, sempre que criar um lote novo: `py agendador_publicacoes.py --todos --sem-instagram`
  — dispara o agendamento nativo de YouTube/Facebook na hora, e **não toca em nenhum job de
  Instagram** (fica "pendente", esperando a nuvem). Esse comando já sincroniza a mídia nova pro
  Supabase sozinho antes de processar (não precisa mais rodar `sincronizar_midia_nuvem.py` à
  parte — ficou embutido, 28/07/2026).
- **Na nuvem** (já configurado no workflow): `python agendador_publicacoes.py --todos --so-instagram`
  — só toca em Instagram, nunca em YouTube/Facebook, e nunca sincroniza (não tem sentido lá,
  não tem D:\ pra sincronizar de lá mesmo).

### 3. Testar sem esperar o cron

Na aba **Actions** do repositório → escolher o workflow "Postar conteudo agendado" →
botão **Run workflow** → Run. Acompanha o log ali mesmo.

## Uso do dia a dia

**Importante:** a partir de agora, os lotes "de verdade" (os que a nuvem processa) vivem AQUI
dentro de `_nuvem_repo/_agendamentos/` — não mais só na pasta antiga
`01_Scripts/_agendamentos/` do pipeline principal. Se um lote novo for criado na pasta antiga,
copie o arquivo JSON pra dentro desta pasta (`_nuvem_repo/_agendamentos/`) antes dos passos
abaixo — senão a nuvem nunca vai saber que ele existe.

Sempre que criar/editar um lote novo (JSON em `_agendamentos/`) com arquivos ainda só no seu D:\:

```bash
py agendador_publicacoes.py --todos --sem-instagram
```

Isso já sincroniza a mídia nova pro Supabase, dispara YouTube/Facebook na hora, e deixa os jobs
de Instagram prontos (com URL da nuvem gravada) esperando o robô. Depois:

```bash
git add .
git commit -m "novo lote da semana"
git push
```

A partir daí o robô na nuvem consegue publicar o Instagram sozinho, no horário certo, sem seu PC.

Depois que os jobs de um lote terminam (todos "publicado"/"agendado"), rodar de vez em quando:

```bash
py sincronizar_midia_nuvem.py --limpar-publicados
git add _agendamentos
git commit -m "limpa midia ja publicada do bucket"
git push
```

Isso libera espaço no bucket permanente do Supabase (evita crescer sem parar).

## Como funciona por baixo dos panos

- **YouTube e Facebook**: têm agendamento nativo — o robô só precisa rodar uma vez perto do
  momento de criar o job pra "avisar" a plataforma, ela publica sozinha depois.
- **Instagram**: não tem agendamento nativo (limitação da própria Meta) — por isso o robô
  precisa checar periodicamente (a cada 15 min) se já chegou a hora de algum post, e aí publica
  na hora certa.
- O script nunca reposta o que já foi feito — cada job vira "publicado"/"agendado" no próprio
  JSON assim que a ação é concluída, e esse status é commitado de volta pro repositório
  automaticamente ao final de cada execução.
