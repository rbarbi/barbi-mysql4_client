# Barbi Mysql4 Client

Interface gráfica para Windows que conecta em servidores **MySQL antigos (3.23, 4.0 e 4.1)**. Ela
existe para resolver problemas de compatibilidade que as IDEs e os drivers atuais não resolvem mais.

[**⬇ Baixar a última versão (BarbiMysql4Client.exe)**](https://github.com/rbarbi/barbi-mysql4_client/releases/latest)

---

## Por que este projeto existe

Ainda há muito sistema legado rodando em MySQL 4.x: ERPs antigos, servidores que ninguém quer mexer,
bancos que só precisam ser consultados de vez em quando. O problema é que as ferramentas modernas
pararam de conversar com esses servidores:

- **Autenticação `OLD_PASSWORD`**: usuários criados no MySQL 4.0 (ou com `old_passwords=1`) usam o
  hash antigo de 16 caracteres. Os drivers atuais recusam esse tipo de login, com erros como
  *"Client does not support authentication protocol requested by server"* ou
  *"Authentication method 'mysql_old_password' not supported"*.
- **Protocolo pré-4.1**: o MySQL 3.23 e o 4.0 usam um handshake mais antigo que os conectores
  modernos (Connector/J, Connector/NET, mysql-connector, PyMySQL...) não implementam mais.
- **IDEs que dependem desses drivers**, como MySQL Workbench, DBeaver, HeidiSQL e similares em
  versões recentes, simplesmente não conectam. Para usar versões antigas delas, você precisa manter
  instaladores velhos e drivers descontinuados.

O **Barbi Mysql4 Client** fala o protocolo do MySQL **diretamente via socket**, sem nenhum driver
externo. Ele implementa os dois handshakes (4.0 e 4.1) e as duas formas de autenticação (old password
e `mysql_native_password`). Por isso ele também conecta em versões mais novas que aceitem
`mysql_native_password`.

## Funcionalidades

- **Cadastro de conexões**: host, porta, usuário, senha, banco padrão e charset (latin1, utf8,
  latin2, ascii). As senhas são salvas **criptografadas com a DPAPI do Windows**, vinculadas ao seu
  usuário.
- **Navegação**: árvore de bancos → tabelas → colunas, com filtro por nome.
- **Navegador de tabela** (duplo clique numa tabela):
  - filtro `WHERE`, `ORDER BY` e paginação;
  - **editar, inserir e excluir** registros, localizando a linha pela chave primária;
  - **Atualizar filtrados**: `UPDATE` de uma coluna em todos os registros do filtro, mostrando antes
    quantos registros serão afetados;
  - clique direito > *Filtrar por este valor*.
- **Segurança nas alterações**: todo comando que altera dados mostra o SQL gerado e pede
  confirmação antes de executar.
- **Editor SQL** com destaque de sintaxe. Executa a seleção ou o script inteiro, aceita várias
  instruções separadas por `;` e mostra cada resultado numa aba. Também abre e salva arquivos `.sql`.
- **Exportação** para Excel (.xlsx), CSV (separado por `;`, abre direto no Excel), JSON e SQL
  (`INSERT`s). No navegador de tabela dá para exportar só a página atual ou todos os registros do
  filtro.
- **Copiar** célula ou linhas, com ou sem cabeçalho, para colar no Excel.

## Instalação

Não há instalação. Baixe o `BarbiMysql4Client.exe` na
[página de releases](https://github.com/rbarbi/barbi-mysql4_client/releases/latest) e execute.
Não é preciso ter Python instalado.

> O Windows SmartScreen pode avisar que o executável não é reconhecido, porque ele não é assinado
> digitalmente. Clique em *Mais informações* → *Executar assim mesmo*.

## Uso rápido

1. **Conexão > Conectar** (`Ctrl+O`): cadastre o servidor e clique em *Testar* para confirmar o acesso.
2. Clique em *Conectar*. Os bancos aparecem na árvore à esquerda.
3. Dê duplo clique numa tabela para navegar e editar os dados, ou abra uma aba SQL com `Ctrl+T`.

| Atalho                 | Ação                                         |
|------------------------|----------------------------------------------|
| `Ctrl+O`               | Conectar                                     |
| `Ctrl+T`               | Nova aba SQL                                 |
| `Ctrl+W`               | Fechar aba                                   |
| `F5` / `Ctrl+Enter`    | Executar a seleção, ou tudo se nada estiver selecionado |
| `Enter` / duplo clique | Editar registro (no navegador de tabela)     |
| `Del`                  | Excluir registro(s) selecionado(s)           |

As conexões ficam em `%APPDATA%\MySQL4Client\conexoes.json`.

## Rodar a partir do código-fonte

Requer Python 3.10+ com Tkinter (já vem no instalador oficial do Python para Windows). O projeto não
tem dependências externas.

```
python mysql4client.py
```

## Gerar o executável

```
build.bat
```

O script cria um venv em `%LOCALAPPDATA%\mysql4client-build`, instala o PyInstaller e gera
`dist\BarbiMysql4Client.exe` (arquivo único).

## Estrutura do código

| Arquivo           | Conteúdo                                                                 |
|-------------------|--------------------------------------------------------------------------|
| `mysql4proto.py`  | Implementação do protocolo MySQL: handshake 4.0/4.1, old/new password, consultas |
| `mysql4client.py` | Interface gráfica (Tkinter)                                              |
| `exporters.py`    | Exportação para xlsx, csv, json e sql                                    |
| `build.bat`       | Geração do executável com PyInstaller                                    |
