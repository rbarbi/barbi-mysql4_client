# Barbi Mysql4 Client

Cliente gráfico para Windows que acessa servidores **MySQL 3.23 / 4.0 / 4.1** (e versões mais novas
que aceitem `mysql_native_password`). Ele fala o protocolo do MySQL diretamente, porque os drivers
atuais não suportam mais o protocolo pré-4.1 nem a autenticação `OLD_PASSWORD`.

## Uso

Execute `dist\BarbiMysql4Client.exe`. Não precisa instalar Python.

- **Conexão > Conectar (Ctrl+O)**: cadastro de conexões (host, porta, usuário, senha, banco padrão,
  charset). A senha é salva criptografada com a DPAPI do Windows em `%APPDATA%\MySQL4Client\conexoes.json`.
- **Menu lateral**: bancos → tabelas → colunas. O campo no topo filtra por nome. Clique direito abre mais opções.
- **Duplo clique numa tabela**: abre o navegador da tabela, com:
  - filtro `WHERE` e `ORDER BY`, além de paginação;
  - **Editar** (duplo clique / Enter), **Inserir** e **Excluir** (Del). A linha é localizada pela chave primária;
  - **Atualizar filtrados...**: `UPDATE` de uma coluna em todos os registros do filtro aplicado,
    mostrando antes quantos registros serão afetados;
  - clique direito > *Filtrar por este valor*.
  - Todo comando de alteração mostra o SQL para confirmação antes de executar.
- **Aba SQL (Ctrl+T)**: editor com F5 / Ctrl+Enter (executa a seleção ou tudo). Aceita várias
  instruções separadas por `;`, e cada resultado aparece numa aba.
- **Exportar**: Excel (.xlsx), CSV (`;`, abre direto no Excel), JSON e SQL (INSERTs). No navegador de
  tabela você pode exportar só a página ou todos os registros do filtro.

## Gerar o executável

```
build.bat
```

O script cria um venv em `%LOCALAPPDATA%\mysql4client-build`, instala o PyInstaller e gera
`dist\BarbiMysql4Client.exe`. Para rodar sem compilar, use `python mysql4client.py` (Python 3.10+ com Tkinter).

## Arquivos

- `mysql4proto.py`: implementação do protocolo (handshake 4.0/4.1, old/new password, consultas)
- `mysql4client.py`: interface (Tkinter)
- `exporters.py`: exportação xlsx/csv/json/sql
