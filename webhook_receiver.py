import os
import time
import re
import threading
from flask import Flask, request, jsonify
from flask_cors import CORS
from waitress import serve
import requests
import urllib3
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# --- FORÇAR DESATIVAÇÃO GLOBAL DE SSL (CORREÇÃO COMPLETA PARA REDE/PROXY) ---
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
old_merge_environment_settings = requests.Session.merge_environment_settings

def patched_merge_environment_settings(self, url, verify, cert, proxies, hooks):
    settings = old_merge_environment_settings(self, url, verify, cert, proxies, hooks)
    settings['verify'] = False  
    return settings

requests.Session.merge_environment_settings = patched_merge_environment_settings
# ----------------------------------------------------------------------------

# --- CONFIGURAÇÕES --- 
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
MILVUS_API_TOKEN = os.getenv('MILVUS_API_TOKEN')

# Validação de segurança para garantir que as variáveis foram carregadas
if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, MILVUS_API_TOKEN]):
    raise ValueError("ERRO: Variáveis de ambiente faltando! Verifique suas configurações.")

# Inicialização do Bot e Flask
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)
CORS(app)

# Variável global para o painel HTML
ultima_notificacao = {"novo": False}

# --- FUNÇÕES AUXILIARES ---
def limpar_html(texto_html):
    if not texto_html: return "Sem descrição"
    texto_limpo = re.sub(r'<[^>]*>', '', str(texto_html))
    return texto_limpo.strip()

# --- ROTAS DO PAINEL NOC ---
@app.route('/get_notificacao', methods=['GET'])
def get_notificacao():
    global ultima_notificacao
    return jsonify(ultima_notificacao)

# --- WEBHOOK MILVUS (RECEBE NOVOS CHAMADOS) ---
@app.route('/milvus_webhook', methods=['POST'])
def handle_webhook():
    global ultima_notificacao
    try:
        data = request.get_json(force=True)
        codigo = str(data.get('codigo_chamado', 'N/A'))
        cliente = str(data.get('cliente_nome', 'N/A'))
        mesa = str(data.get('mesa_trabalho', 'N/A'))
        assunto = str(data.get('assunto', 'N/A'))
        descricao_limpa = limpar_html(data.get('descrição', ''))

        # 1. ATUALIZA O PAINEL HTML
        ultima_notificacao = {
            "id_chamado": codigo, 
            "dados": {
                "codigo": codigo,
                "cliente": cliente,
                "assunto": assunto,
                "mesa": mesa,
                "descricao": descricao_limpa
            }
        }

        # 2. MONTA A MENSAGEM DO TELEGRAM
        msg = (
            f"🔔 *NOVO CHAMADO RECEBIDO*\n\n"
            f"🎫 *Código:* `{codigo}`\n"
            f"🏢 *Cliente:* {cliente}\n"
            f"🖥️ *Mesa:* {mesa}\n"
            f"📝 *Assunto:* {assunto}\n"
            f"📖 *Descrição:* _{descricao_limpa}_"
        )
        
        # Cria o botão de ação rápida anexado à mensagem
        markup_finalizar = InlineKeyboardMarkup()
        markup_finalizar.add(InlineKeyboardButton("✅ Finalizar Chamado", callback_data=f"finalizar_{codigo}"))

        # Envia ao grupo/canal
        bot.send_message(TELEGRAM_CHAT_ID, msg, parse_mode='Markdown', reply_markup=markup_finalizar)
        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print(f"❌ Erro no Webhook Milvus: {e}")
        return jsonify({"status": "erro"}), 500

# --- INTERAÇÃO COM BOTÕES DO TELEGRAM ---
@bot.callback_query_handler(func=lambda call: True)
def query_handler(call):
    try:
        # Captura o clique no botão de finalizar chamado
        if call.data.startswith("finalizar_"):
            codigo_chamado = call.data.split("_")[1]
            
            # Limpa o estado de "carregando" do botão no Telegram
            bot.answer_callback_query(call.id)
            
            # Pergunta o motivo para o técnico no chat
            msg_pergunta = bot.send_message(
                call.message.chat.id, 
                f"✍️ *Qual o motivo do fechamento do chamado `{codigo_chamado}`?*\n_(Digite sua resposta abaixo e envie)_", 
                parse_mode='Markdown'
            )
            
            # Prepara o bot para escutar a próxima resposta do técnico e direcionar para a função de envio
            bot.register_next_step_handler(msg_pergunta, processar_fechamento, codigo_chamado, call.message)
                
    except Exception as e:
        print(f"Erro no callback_query: {e}")
        bot.answer_callback_query(call.id, "Erro ao processar requisição.")

# --- FUNÇÃO QUE VALIDA E ENVIA OS DADOS PARA A MILVUS ---
def processar_fechamento(message, codigo_chamado, mensagem_original):
    try:
        motivo_digitado = message.text  # Captura o que o usuário acabou de digitar
        
        bot.send_message(message.chat.id, f"⏳ Integrando com a Milvus para fechar o chamado `{codigo_chamado}`...", parse_mode='Markdown')
        
        url_milvus = "https://apiintegracao.milvus.com.br/api/chamado/finalizar"
        
        # Payload montado exatamente conforme a documentação oficial da Milvus (PUT)
        payload = {
            "chamado_codigo": str(codigo_chamado),
            "chamado_servico_realizado": motivo_digitado,
            "chamado_equipamento_retirado": "Nenhum",
            "chamado_material_utilizado": "Nenhum"
        }
        
        headers_milvus = {
            'Content-Type': 'application/json',
            'Authorization': MILVUS_API_TOKEN
        }
        
        # Envia a requisição usando PUT
        response = requests.put(url_milvus, json=payload, headers=headers_milvus)
        
        # 200, 201 ou 204 significam que a Milvus aceitou e fechou o chamado com sucesso
        if response.status_code in [200, 201, 204]:
            bot.send_message(message.chat.id, f"✅ *Sucesso!* Chamado `{codigo_chamado}` foi finalizado no sistema.", parse_mode='Markdown')
            
            # Atualiza o card de notificação original: remove o botão e escreve a solução adotada
            texto_atualizado = mensagem_original.text + f"\n\n🟢 *Finalizado pelo Telegram:*\n_{motivo_digitado}_"
            bot.edit_message_text(texto_atualizado, mensagem_original.chat.id, mensagem_original.message_id, parse_mode='Markdown')
        else:
            # Caso dê erro (ex: Chamado ainda em status 'Novo' e não em 'Atendimento')
            bot.send_message(
                message.chat.id, 
                f"❌ *Falha ao finalizar chamado `{codigo_chamado}`.*\n\n"
                f"⚠️ *Nota:* O chamado obrigatoriamente precisa estar com o status *'Em Atendimento'* antes de ser fechado.\n\n"
                f"Status de Retorno da API: {response.status_code}\nResposta: {response.text}",
                parse_mode='Markdown'
            )
            
    except Exception as e:
        print(f"Erro no processar_fechamento: {e}")
        bot.send_message(message.chat.id, f"❌ Ocorreu um erro interno ao processar o fechamento: {e}")

# --- INICIALIZAÇÃO CONTÍNUA ---
def executar_bot_telegram():
    while True:
        try:
            print("🔄 Removendo Webhooks antigos do Telegram...")
            bot.remove_webhook()
            time.sleep(1)
            
            print("🤖 Bot Telegram conectado e aguardando interações...")
            bot.infinity_polling(timeout=90, long_polling_timeout=60)
        except Exception as e:
            print(f"🚨 Conexão com Telegram falhou: {e}. Reiniciando em 5 segundos...")
            time.sleep(5)

if __name__ == '__main__':
    # 1. Inicia o Polling do Bot em uma Thread separada
    thread_bot = threading.Thread(target=executar_bot_telegram, daemon=True)
    thread_bot.start()
    
    # 2. Inicia o Servidor Flask na porta 5000 para receber os Webhooks da Milvus
    print("🚀 SISTEMA NOC ONLINE - PORTA 5000")
    serve(app, host='0.0.0.0', port=5000, threads=10)