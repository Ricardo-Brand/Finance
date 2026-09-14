# Planejador Financeiro

App local em Python (Flask) para organizar suas contas mês a mês, com contas
parceladas se repetindo automaticamente e controle do que você tem a receber.

## Como rodar

1. Instale as dependências (só o Flask):
   ```
   pip install -r requirements.txt
   ```

2. Rode o app:
   ```
   python app.py
   ```

3. Abra no navegador: http://127.0.0.1:5000

Os dados ficam salvos em `finance.db` (SQLite), na mesma pasta do app. Pode
fechar e abrir o app quantas vezes quiser que as informações continuam lá.

## Como usar

- Na página inicial você vê os 12 meses do ano atual, cada card com o total
  de contas e o total a receber daquele mês. Use as setas para navegar entre
  anos.
- Clique em um mês para ver a tabela detalhada, criar, editar ou excluir
  itens.
- Ao criar um item, marque "Parcelado" e informe quantas parcelas — o valor
  informado é o valor de cada parcela, e ele será lançado automaticamente
  nos meses seguintes, sem precisar adicionar um por um.
- Itens parcelados podem ser excluídos só naquele mês ("Excluir") ou junto
  com todas as parcelas futuras ("Excluir futuras").
