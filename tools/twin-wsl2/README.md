# TWIN no WSL2

Rode o firmware WROOM atual no simulador nativo TWIN, com a matriz HUB75 128×64 no navegador. O simulador, ROM e estado persistente ficam em `~/.local/share/animatedpixelclock-twin`; nada disso entra no repositório.

## 1. Habilitar localhost entre Windows e WSL

No Windows 11 22H2 ou posterior, abra `%UserProfile%\.wslconfig` e adicione a seção abaixo se ainda não existir. Se já existir `[wsl2]`, acrescente somente a linha `networkingMode`:

```ini
[wsl2]
networkingMode=mirrored
```

Em PowerShell, aplique a configuração:

```powershell
wsl --shutdown
```

Inicie novamente a distro Ubuntu. O modo Mirrored permite que o Companion Windows e os listeners no WSL alcancem-se por `127.0.0.1`. Ele também pode permitir conexões da LAN, mas este setup não cria regras de firewall e os listeners permanecem presos a loopback.

## 2. Iniciar o simulador

No WSL:

```bash
cd ~/AnimatedPixelClock
./tools/twin-wsl2/run-with-companion.sh
```

O script compila `matrix-s3-wroom-twin`, um perfil exclusivo do simulador que liga `Serial` à USB Serial/JTAG (o perfil físico `matrix-s3-wroom` continua usando UART0/CH343), inicializa/atualiza a flash virtual preservando NVS e LittleFS, e executa `esp32sim --board hub75-panel` com 16 MB de flash e 8 MB de PSRAM. Na primeira execução ele clona/builda TWIN, baixa a ROM oficial ESP32-S3 se necessário e provisiona a rede **virtual** `PixelClock-TWIN` pelo Improv-Serial. Essa rede não é a rede Wi-Fi doméstica; não coloque nela credenciais reais. O Companion chega à API e ao listener UDP por encaminhamento local do TWIN.

Esse launcher pede autenticação `sudo` uma vez, inicia a ponte HTTP na porta 80 e então inicia o simulador. Ao encerrar o launcher, ele também para a ponte.

Abra no navegador do Windows:

- Painel HUB75: <http://127.0.0.1:8790/panel.html>
- Portal/API: <http://127.0.0.1:8080/>

Para apagar somente a flash virtual e reprovisionar um chip novo:

```bash
./tools/twin-wsl2/run-with-companion.sh --fresh
```

O script pede confirmação antes de apagar `~/.local/share/animatedpixelclock-twin/state/flash.bin`.

Para executar somente o simulador, sem a ponte HTTP, use `./tools/twin-wsl2/run.sh`.

## 3. Conectar o Companion v4 Windows sem alterar o `.exe`

O TWIN encaminha UDP `127.0.0.1:4210` para a porta 4210 do firmware e TCP `127.0.0.1:8080` para a API HTTP guest:80. O launcher `run-with-companion.sh` inicia a ponte loopback que o Companion precisa, pois ele usa HTTP na porta 80 sem opção de porta. Se executar apenas `run.sh`, inicie a ponte manualmente em outro terminal WSL:

```bash
sudo python3 ~/AnimatedPixelClock/tools/twin-wsl2/http_bridge.py
```

A ponte aceita somente `127.0.0.1:80` e envia para `127.0.0.1:8080`. Não a execute com opções para escutar em `0.0.0.0`. Se a porta 80 estiver ocupada, pare o serviço local que a ocupa antes; não abra uma regra de firewall para contornar o conflito.

No Companion `.exe`, configure o endereço do dispositivo como `127.0.0.1` e a porta UDP como `4210`. O Companion permanece no Windows; não precisa ser reinstalado nem modificado.

## Pré-requisitos e resolução de problemas

- Ubuntu/WSL2, Git, Python 3, Rust/Cargo (stable) e PlatformIO Core. Para PlatformIO em virtualenv, o launcher encontra `~/.venvs/pio/bin/pio`; caso contrário, ajuste `PIO=/caminho/para/pio`.
- Se o navegador não abrir a página, confira no log do TWIN `board UI: http://127.0.0.1:8790/` e se o painel está transmitindo frames. A tela do simulador pode precisar de alguns segundos após o boot.
- Teste a API dentro do WSL com `curl http://127.0.0.1:8080/api/status` e a ponte com `curl http://127.0.0.1/api/status`. Depois valide no próprio Companion o HTTP e o streaming UDP: `localhost` compartilhado não garante a entrega UDP em toda instalação; Mirrored e versão atual do WSL precisam estar ativos.
- Se a API local funciona no WSL, mas o Companion no Windows não conecta, confirme `wsl --shutdown` após editar `.wslconfig`, que UDP 4210 não está bloqueada, e que o processo não foi iniciado em modo NAT. Não exponha os listeners à LAN.
- O build usa os arquivos de saída do PlatformIO no ambiente `matrix-s3-wroom`; as atualizações do app são gravadas no slot app0, e a partição NVS/LittleFS do simulador é preservada. O flash TWIN não compartilha estado com o dispositivo físico.
