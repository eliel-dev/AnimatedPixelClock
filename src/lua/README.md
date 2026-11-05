# Efeitos Lua pela rede local

Este firmware habilita efeitos Lua somente no perfil `matrix-s3-wroom`, destinado ao ESP32-S3 N16R8 (16 MB de flash e 8 MB de PSRAM). O runtime, a API de desenho e o sandbox vêm do subsistema Lua do NickoScope, adaptados ao display HUB75 e ao portal deste projeto.

## Como usar

1. Conecte o navegador à mesma rede local do relógio e abra o portal.
2. Entre em **Efeitos Lua** e carregue a galeria pública.
3. Escolha **Adicionar ao relógio**. O navegador busca o script no GitHub e envia o arquivo ao ESP32 pela rede local; o ESP32 não precisa de acesso à internet.
4. Na lista de instalados, use **Mostrar** para exibir o efeito ou **Excluir** para removê-lo. **Encerrar efeito** volta à tela de relógio. Selecionar outro modo de tela também encerra o efeito.

A galeria depende de internet no aparelho que abriu o portal. Se o GitHub estiver inacessível, os efeitos já instalados continuam disponíveis.

## Armazenamento e limites

- Os scripts ficam em `/lua` no LittleFS. As animações PCA existentes continuam em `/anim`; o particionamento não muda.
- São aceitos até 36 scripts. O limite técnico por arquivo é 512 KiB, limitado também pelo espaço livre do LittleFS, mantendo 512 KiB reservados para os demais dados.
- O sandbox aceita somente texto Lua, verifica a estrutura antes de gravar e testa o efeito no painel antes de adicioná-lo à lista.
- A validação limita a profundidade de blocos e delimitadores a 16. O runtime também aplica orçamentos de instruções, tempo de carregamento, tempo por quadro e heap Lua; `pcall`, `xpcall`, acesso a arquivos e outras funções perigosas não são expostas.
- Os quadros são desenhados num canvas RGB888 de 128×64 alocado na PSRAM e convertidos para RGB565 ao chegar ao HUB75.

## API local

- `GET /api/lua` lista os efeitos instalados, a seleção atual e o espaço disponível.
- `POST /api/lua` aceita `{"show": 0}`, `{"delete": "nome_do_efeito"}` ou `{"stop": true}`.
- `POST /api/lua/upload?name=nome_do_efeito` recebe um arquivo Lua em uma parte multipart chamada `script`. O nome aceita de 1 a 24 letras, números e sublinhados.

A galeria usada pelo portal é [`NickoScope/AnimatedPixelClock/gallery`](https://github.com/NickoScope/AnimatedPixelClock/tree/main/gallery). Ela é lida pelo navegador; o firmware não consulta serviços externos.
