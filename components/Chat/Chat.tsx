import {
  IconClearAll,
  IconInfoCircle,
  IconLogout,
  IconUser,
} from '@tabler/icons-react';
import {
  MutableRefObject,
  memo,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from 'react';
import toast from 'react-hot-toast';

import { useTranslation } from 'next-i18next';

import { getEndpoint } from '@/utils/app/api';
import {
  saveConversation,
  saveConversations,
  updateConversation,
} from '@/utils/app/conversation';
import { throttle } from '@/utils/data/throttle';

import { ChatBody, Conversation, Message } from '@/types/chat';
import { Plugin } from '@/types/plugin';

import HomeContext from '@/pages/api/home/home.context';

import { ChatInput } from './ChatInput';
import { ChatLoader } from './ChatLoader';
import { ErrorMessageDiv } from './ErrorMessageDiv';
import { MemoizedChatMessage } from './MemoizedChatMessage';

interface Props {
  stopConversationRef: MutableRefObject<boolean>;
}

export const Chat = memo(({ stopConversationRef }: Props) => {
  const { t } = useTranslation('chat');

  const {
    state: {
      selectedConversation,
      conversations,
      apiKey,
      serverSideApiKeyIsSet,
      messageIsStreaming,
      modelError,
      loading,
    },
    handleUpdateConversation,
    signOut,
    user,
    dispatch: homeDispatch,
  } = useContext(HomeContext);

  const [currentMessage, setCurrentMessage] = useState<Message>();
  const [autoScrollEnabled, setAutoScrollEnabled] = useState<boolean>(true);
  const [showScrollDownButton, setShowScrollDownButton] =
    useState<boolean>(false);

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const initChartData = (propMessage: any) => {
    if (!propMessage || !propMessage.chartData) return null;
    return propMessage.chartData;
  };
  const handleSend = useCallback(
    async (message: Message, deleteCount = 0, plugin: Plugin | null = null) => {
      // todo get user id
      if (selectedConversation) {
        let updatedConversation: Conversation;
        if (deleteCount) {
          const updatedMessages = [...selectedConversation.messages];
          for (let i = 0; i < deleteCount; i++) {
            updatedMessages.pop();
          }
          updatedConversation = {
            ...selectedConversation,
            messages: [...updatedMessages, message],
          };
        } else {
          updatedConversation = {
            ...selectedConversation,
            messages: [...selectedConversation.messages, message],
          };
        }
        homeDispatch({
          field: 'selectedConversation',
          value: updatedConversation,
        });
        homeDispatch({ field: 'loading', value: true });
        homeDispatch({ field: 'messageIsStreaming', value: true });
        const chatBody: ChatBody = {
          messages: updatedConversation.messages,
          userId: user.username,
          modelType: plugin?.id.includes('-hard')
            ? 'bedrock-hard'
            : 'bedrock-normal',
          modelId: plugin?.id.includes('claude2')
            ? 'anthropic-claude2'
            : plugin?.id.includes('-haiku')
            ? 'anthropic-claude3-haiku'
            : 'anthropic-claude3',
        };
        const endpoint = getEndpoint();
        const body = JSON.stringify(chatBody);
        const controller = new AbortController();
        const signal = controller.signal;
        let shouldAbort = false;
        const checkAbort = () => {
          if (stopConversationRef.current === true) {
            try {
              shouldAbort = true;
              if (signal.aborted && signal.reason) {
                controller.abort();
              }
            } catch (e) {}
            homeDispatch({ field: 'loading', value: false });
            homeDispatch({ field: 'messageIsStreaming', value: false });
          }
        };
        checkAbort();
        const intervalId = setInterval(() => {
          checkAbort();
          if (stopConversationRef.current === true) {
            clearInterval(intervalId); // 停止轮询
          }
        }, 100); // 每 100ms 检查一次

        if (shouldAbort) {
          return;
        }
        const response = await fetch(endpoint, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          signal,
          body,
        });
        if (stopConversationRef.current === true) {
          controller.abort();
          homeDispatch({ field: 'loading', value: false });
          homeDispatch({ field: 'messageIsStreaming', value: false });
          return;
        }
        // console.log('response', response);
        if (!response.ok) {
          homeDispatch({ field: 'loading', value: false });
          homeDispatch({ field: 'messageIsStreaming', value: false });
          toast.error(response.statusText);
          return;
        }
        const data = response.body;
        if (!data) {
          homeDispatch({ field: 'loading', value: false });
          homeDispatch({ field: 'messageIsStreaming', value: false });
          return;
        }
        // if (!plugin) {
        if (updatedConversation.messages.length === 1) {
          const { content } = message;
          const customName =
            content.length > 30 ? content.substring(0, 30) + '...' : content;
          updatedConversation = {
            ...updatedConversation,
            name: customName,
          };
        }
        homeDispatch({ field: 'loading', value: false });
        const reader = data.getReader();
        const decoder = new TextDecoder();
        let done = false;
        let isFirst = true;
        let text = '';
        // 请求成功时，停止轮询
        clearInterval(intervalId);
        if (shouldAbort) {
          homeDispatch({ field: 'loading', value: false });
          homeDispatch({ field: 'messageIsStreaming', value: false });
          return;
        }
        while (!done) {
          const { value, done: doneReading } = await reader.read();
          done = doneReading;
          const chunkValue = decoder.decode(value);
          text += chunkValue;

          let answerObj = JSON.parse(text);
          if (!answerObj || Object.keys(answerObj).length === 0) {
            answerObj = { content: '暂无回答' };
          }
          console.log('requestId ----> ', answerObj.requestId);
          let chartData = initChartData(answerObj);
          let showContent = '';
          if (answerObj.sql) {
            showContent = `原始SQL:\n \`\`\`sql \n${answerObj.sql} \n\`\`\`\n `;
          }
          if (answerObj.content) {
            showContent += answerObj.content + '\n';
          }
          if (answerObj.mdData) {
            showContent += answerObj.mdData + '\n';
          }

          if (isFirst) {
            isFirst = false;
            const updatedMessages: Message[] = [
              ...updatedConversation.messages,
              {
                role: 'assistant',
                content: showContent,
                sql: answerObj.sql,
                chartData,
                chartType: answerObj.chartType,
                extra: answerObj.extra,
              },
            ];
            updatedConversation = {
              ...updatedConversation,
              messages: updatedMessages,
            };
            homeDispatch({
              field: 'selectedConversation',
              value: updatedConversation,
            });
          } else {
            const updatedMessages: Message[] = updatedConversation.messages.map(
              (message, index) => {
                if (index === updatedConversation.messages.length - 1) {
                  return {
                    ...message,
                    content: showContent,
                    sql: answerObj.sql,
                    chartData,
                    chartType: answerObj.chartType,
                    extra: answerObj.extra,
                  };
                }
                return message;
              },
            );
            updatedConversation = {
              ...updatedConversation,
              messages: updatedMessages,
            };
            homeDispatch({
              field: 'selectedConversation',
              value: updatedConversation,
            });
          }
        }
        // while (!done) {
        //   console.log(
        //     'stopConversationRef.current while',
        //     stopConversationRef.current,
        //   );

        //   const abortPromise = new Promise((_, reject) => {
        //     const abortListener = () => {
        //       reject(new Error('Request aborted'));
        //     };
        //     controller.signal.addEventListener('abort', abortListener);
        //   });

        //   try {
        //     const { value, done: doneReading } = (await Promise.race([
        //       reader.read(),
        //       abortPromise,
        //     ])) as any;
        //     console.log(
        //       'stopConversationRef.current while222',
        //       stopConversationRef.current,
        //     );
        //     if (stopConversationRef.current === true) {
        //       controller.abort();
        //       done = true;
        //       break;
        //     }

        //     done = doneReading;
        //     const chunkValue = decoder.decode(value);
        //     text += chunkValue;
        //     console.log(
        //       'stopConversationRef.current while333',
        //       stopConversationRef.current,
        //     );
        //     let answerObj = JSON.parse(text);
        //     if (!answerObj || Object.keys(answerObj).length === 0) {
        //       answerObj = { content: '暂无回答' };
        //     }

        //     console.log('requestId ----> ', answerObj.requestId);
        //     let chartData = initChartData(answerObj);
        //     let showContent = '';
        //     if (answerObj.sql) {
        //       showContent = `原始SQL:\n \`\`\`sql \n${answerObj.sql} \n\`\`\`\n `;
        //     }
        //     if (answerObj.content) {
        //       showContent += answerObj.content + '\n';
        //     }
        //     if (answerObj.mdData) {
        //       showContent += answerObj.mdData + '\n';
        //     }

        //     if (isFirst) {
        //       isFirst = false;
        //       const updatedMessages: Message[] = [
        //         ...updatedConversation.messages,
        //         {
        //           role: 'assistant',
        //           content: showContent,
        //           sql: answerObj.sql,
        //           chartData,
        //           chartType: answerObj.chartType,
        //         },
        //       ];
        //       updatedConversation = {
        //         ...updatedConversation,
        //         messages: updatedMessages,
        //       };
        //       homeDispatch({
        //         field: 'selectedConversation',
        //         value: updatedConversation,
        //       });
        //     } else {
        //       const updatedMessages: Message[] =
        //         updatedConversation.messages.map((message, index) => {
        //           if (index === updatedConversation.messages.length - 1) {
        //             return {
        //               ...message,
        //               content: showContent,
        //               sql: answerObj.sql,
        //               chartData,
        //               chartType: answerObj.chartType,
        //             };
        //           }
        //           return message;
        //         });
        //       updatedConversation = {
        //         ...updatedConversation,
        //         messages: updatedMessages,
        //       };
        //       homeDispatch({
        //         field: 'selectedConversation',
        //         value: updatedConversation,
        //       });
        //     }
        //   } catch (error: any) {
        //     if (error.message === 'Request aborted') {
        //       console.log('请求已中止');
        //       done = true;
        //     } else {
        //       console.error('发生错误:', error);
        //     }
        //   }
        // }
        saveConversation(updatedConversation);
        const updatedConversations: Conversation[] = conversations.map(
          (conversation) => {
            if (conversation.id === selectedConversation.id) {
              return updatedConversation;
            }
            return conversation;
          },
        );
        if (updatedConversations.length === 0) {
          updatedConversations.push(updatedConversation);
        }
        homeDispatch({ field: 'conversations', value: updatedConversations });
        saveConversations(updatedConversations);
        homeDispatch({ field: 'messageIsStreaming', value: false });
        // } else {
        //   const { answer } = await response.json();
        //   const updatedMessages: Message[] = [
        //     ...updatedConversation.messages,
        //     { role: 'assistant', content: answer },
        //   ];
        //   updatedConversation = {
        //     ...updatedConversation,
        //     messages: updatedMessages,
        //   };
        //   homeDispatch({
        //     field: 'selectedConversation',
        //     value: updateConversation,
        //   });
        //   saveConversation(updatedConversation);
        //   const updatedConversations: Conversation[] = conversations.map(
        //     (conversation) => {
        //       if (conversation.id === selectedConversation.id) {
        //         return updatedConversation;
        //       }
        //       return conversation;
        //     },
        //   );
        //   if (updatedConversations.length === 0) {
        //     updatedConversations.push(updatedConversation);
        //   }
        //   homeDispatch({ field: 'conversations', value: updatedConversations });
        //   saveConversations(updatedConversations);
        //   homeDispatch({ field: 'loading', value: false });
        //   homeDispatch({ field: 'messageIsStreaming', value: false });
        // }
      }
    },
    [apiKey, conversations, selectedConversation, stopConversationRef],
  );

  const scrollToBottom = useCallback(() => {
    if (autoScrollEnabled) {
      messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
      textareaRef.current?.focus();
    }
  }, [autoScrollEnabled]);

  const handleScroll = () => {
    if (chatContainerRef.current) {
      const { scrollTop, scrollHeight, clientHeight } =
        chatContainerRef.current;
      const bottomTolerance = 30;

      if (scrollTop + clientHeight < scrollHeight - bottomTolerance) {
        setAutoScrollEnabled(false);
        setShowScrollDownButton(true);
      } else {
        setAutoScrollEnabled(true);
        setShowScrollDownButton(false);
      }
    }
  };

  const handleScrollDown = () => {
    chatContainerRef.current?.scrollTo({
      top: chatContainerRef.current.scrollHeight,
      behavior: 'smooth',
    });
  };

  const onClearAll = () => {
    if (
      confirm(t<string>('Are you sure you want to clear all messages?')) &&
      selectedConversation
    ) {
      handleUpdateConversation(selectedConversation, {
        key: 'messages',
        value: [],
      });
    }
  };

  const onShowTips = () => {
    alert(
      '如果查询数据没出来，可以刷新浏览器多尝试几次；如果还是不行，可以联系 汤帅 。',
    );
  };

  const scrollDown = () => {
    if (autoScrollEnabled) {
      messagesEndRef.current?.scrollIntoView(true);
    }
  };
  const throttledScrollDown = throttle(scrollDown, 250);

  // useEffect(() => {
  //   console.log('currentMessage', currentMessage);
  //   if (currentMessage) {
  //     handleSend(currentMessage);
  //     homeDispatch({ field: 'currentMessage', value: undefined });
  //   }
  // }, [currentMessage]);

  useEffect(() => {
    throttledScrollDown();
    selectedConversation &&
      setCurrentMessage(
        selectedConversation.messages[selectedConversation.messages.length - 2],
      );
  }, [selectedConversation, throttledScrollDown]);

  useEffect(() => {
    const observer = new IntersectionObserver(
      ([entry]) => {
        setAutoScrollEnabled(entry.isIntersecting);
        if (entry.isIntersecting) {
          textareaRef.current?.focus();
        }
      },
      {
        root: null,
        threshold: 0.5,
      },
    );
    const messagesEndElement = messagesEndRef.current;
    if (messagesEndElement) {
      observer.observe(messagesEndElement);
    }
    return () => {
      if (messagesEndElement) {
        observer.unobserve(messagesEndElement);
      }
    };
  }, [messagesEndRef]);

  return (
    <div className="relative flex-1 overflow-hidden bg-white dark:bg-[#343541]">
      {!(apiKey || serverSideApiKeyIsSet) ? (
        <div className="mx-auto flex h-full w-[300px] flex-col justify-center space-y-6 sm:w-[600px]">
          <div className="text-center text-4xl font-bold text-black dark:text-white">
            欢迎使用
          </div>
          <div className="text-center text-lg text-black dark:text-white">
            <div className="mb-8">请在下方输入您的问题，或参考右侧提示。</div>
            <div className="mb-2 font-bold"></div>
          </div>
        </div>
      ) : modelError ? (
        <ErrorMessageDiv error={modelError} />
      ) : (
        <>
          <div
            className="max-h-full overflow-x-hidden"
            ref={chatContainerRef}
            onScroll={handleScroll}
          >
            {selectedConversation?.messages.length === 0 ? (
              <>
                <div className="mx-auto flex flex-col space-y-5 md:space-y-10 px-3 pt-5 md:pt-12 sm:max-w-[600px]">
                  <div className="text-center text-3xl font-semibold text-gray-800 dark:text-gray-100">
                    Text to SQL
                  </div>
                  <span
                    style={{
                      fontWeight: '400',
                      fontSize: '16px',
                      textAlign: 'center',
                    }}
                  >
                    请在下方输入您的内容，或参考右侧提示。
                  </span>
                </div>
              </>
            ) : (
              <>
                <div className="sticky top-0 z-10 flex justify-center border border-b-neutral-300 bg-neutral-100 py-2 text-sm text-neutral-500 dark:border-none dark:bg-[#444654] dark:text-neutral-200">
                  <IconUser size={18} />
                  {user.username}
                  &nbsp;&nbsp;&nbsp;&nbsp;
                  <></>
                  <button
                    className="ml-2 cursor-pointer hover:opacity-50"
                    onClick={signOut}
                  >
                    <span style={{ display: 'inline' }}>
                      <IconLogout size={18} style={{ display: 'inline' }} />
                      退出
                    </span>
                  </button>
                  &nbsp;&nbsp;&nbsp;&nbsp;
                  <></>
                  <button
                    className="ml-2 cursor-pointer hover:opacity-50"
                    onClick={onClearAll}
                  >
                    <span style={{ display: 'inline' }}>
                      <IconClearAll size={18} style={{ display: 'inline' }} />
                      清除所有消息
                    </span>
                  </button>
                  &nbsp;&nbsp;&nbsp;&nbsp;
                  <button
                    className="ml-2 cursor-pointer hover:opacity-50"
                    onClick={onShowTips}
                  >
                    <span style={{ display: 'inline' }}>
                      <IconInfoCircle size={18} style={{ display: 'inline' }} />
                      查询提示
                    </span>
                  </button>
                </div>

                {selectedConversation?.messages.map((message, index) => (
                  <MemoizedChatMessage
                    key={index}
                    message={message}
                    messageIndex={index}
                    onEdit={(editedMessage) => {
                      setCurrentMessage(editedMessage);
                      // discard edited message and the ones that come after then resend
                      handleSend(
                        editedMessage,
                        selectedConversation?.messages.length - index,
                      );
                    }}
                  />
                ))}

                {loading && <ChatLoader />}

                <div
                  className="h-[162px] bg-white dark:bg-[#343541]"
                  ref={messagesEndRef}
                />
              </>
            )}
          </div>

          <ChatInput
            stopConversationRef={stopConversationRef}
            textareaRef={textareaRef}
            onSend={(message, plugin) => {
              setCurrentMessage(message);
              handleSend(message, 0, plugin);
            }}
            onScrollDownClick={handleScrollDown}
            onRegenerate={() => {
              if (currentMessage) {
                handleSend(currentMessage, 2, null);
              }
            }}
            showScrollDownButton={showScrollDownButton}
          />
        </>
      )}
    </div>
  );
});
Chat.displayName = 'Chat';
