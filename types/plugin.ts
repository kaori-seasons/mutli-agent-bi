import { KeyValuePair } from './data';

export interface Plugin {
  id: PluginID;
  name: PluginName;
  requiredKeys: KeyValuePair[];
}

export interface PluginKey {
  pluginId: PluginID;
  requiredKeys: KeyValuePair[];
}

export enum PluginID {
  Cladue3Hard = 'claude3-hard',
  // Cladue3Haiku = 'claude3-haiku',
  // Cladue2 = 'claude2',
  // Cladue2Hard = 'claude2-hard',
}

export enum PluginName {
  Cladue3Hard = 'Amazon Bedrock(Claude3 增强模式)',
  // Cladue3Haiku = 'Amazon Bedrock(Claude3 极速模式)',
  Cladue2 = 'Amazon Bedrock(Claude2)',
  Cladue2Hard = 'Amazon Bedrock(Claude2 Hard)',
}

export const Plugins: Record<PluginID, Plugin> = {
  [PluginID.Cladue3Hard]: {
    id: PluginID.Cladue3Hard,
    name: PluginName.Cladue3Hard,
    requiredKeys: [
      {
        key: 'BEDROCK_KEY',
        value: '',
      },
    ],
  },
  // [PluginID.Cladue3Haiku]: {
  //   id: PluginID.Cladue3Haiku,
  //   name: PluginName.Cladue3Haiku,
  //   requiredKeys: [
  //     {
  //       key: 'BEDROCK_KEY',
  //       value: '',
  //     },
  //   ],
  // },
  // [PluginID.Cladue2]: {
  //   id: PluginID.Cladue2,
  //   name: PluginName.Cladue2,
  //   requiredKeys: [
  //     {
  //       key: 'BEDROCK_KEY',
  //       value: '',
  //     },
  //   ],
  // },
  // [PluginID.Cladue2Hard]: {
  //   id: PluginID.Cladue2Hard,
  //   name: PluginName.Cladue2Hard,
  //   requiredKeys: [
  //     {
  //       key: 'BEDROCK_KEY',
  //       value: '',
  //     },
  //   ],
  // },
};

export const PluginList = Object.values(Plugins);
