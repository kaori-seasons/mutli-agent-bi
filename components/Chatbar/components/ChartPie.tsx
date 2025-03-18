import { memo, useContext, useEffect, useState } from 'react';

import { Message } from '@/types/chat';

import HomeContext from '@/pages/api/home/home.context';

import { Box, PieChart } from '@cloudscape-design/components';

const ChartPie = memo((props: { message: Message; oldMessage: Message }) => {
  const { message, oldMessage } = props;
  const {
    state: { lightMode },
  } = useContext(HomeContext);
  const [showData, setShowData] = useState([] as any);
  try {
    const chartDataObj =
      message.chartData && typeof message.chartData === 'string'
        ? JSON.parse(message.chartData || '{}')
        : message.chartData;
    if (!chartDataObj || Object.keys(chartDataObj).length <= 1) {
      return <></>;
    }
    const getInitChartData = () => {
      if (!chartDataObj) return;
      const tempShowData: any = [];
      const keys = Object.keys(chartDataObj);
      const defaultType = chartDataObj.index_type
        ? chartDataObj.index_type['0']
        : keys.indexOf('type') > -1
        ? chartDataObj[keys[keys.indexOf('type')]]['0']
        : '';
      if (chartDataObj.entity_name) {
        const valueKeyList = Object.keys(chartDataObj.entity_name);

        if (chartDataObj.index_value) {
          valueKeyList.forEach((item, index) => {
            tempShowData.push({
              title: chartDataObj.entity_name[item],
              value: chartDataObj.index_value[item],
              name: defaultType,
            });
          });
        } else {
          const valuesObj = getDefaultValue(chartDataObj);
          valueKeyList.forEach((item, index) => {
            tempShowData.push({
              title: chartDataObj.entity_name[item],
              value: valuesObj[item],
              name: defaultType,
            });
          });
        }
      } else {
        const objectKeys = Object.keys(chartDataObj);
        let keyIndex = -1;
        objectKeys.forEach((item, index) => {
          if (item.indexOf('name') > -1) {
            keyIndex = index;
          }
        });
        let keyValueIndex: any = {};
        if (keyIndex < 0) {
          keyValueIndex = chartDataObj[objectKeys[0]];
        }
        keyValueIndex = chartDataObj[objectKeys[keyIndex]];
        const valuesObj = getDefaultValue(chartDataObj, true);
        const valueKeyList = Object.keys(keyValueIndex || {});
        valueKeyList.forEach((item, index) => {
          tempShowData.push({
            title: keyValueIndex[item],
            value: valuesObj[item],
          });
        });
      }
      setShowData(tempShowData);
      return;
    };

    const getDefaultValue = (
      chartDataObj: { [x: string]: any },
      noKey = false,
    ) => {
      const keyList = Object.keys(chartDataObj);
      let valueIndex = -1;
      keyList.forEach((item, index) => {
        if (item.indexOf('value') > -1) {
          valueIndex = index;
        }
      });
      if (valueIndex < 0) {
        return chartDataObj[keyList[noKey ? 1 : 0]];
      }
      return chartDataObj[keyList[valueIndex]];
    };

    // eslint-disable-next-line react-hooks/rules-of-hooks
    useEffect(() => {
      getInitChartData();
    }, []);

    if (!chartDataObj) {
      return <></>;
    }

    return showData && showData.length > 0 ? (
      <div className={lightMode === 'dark' ? 'dark-mode-chart' : ''}>
        <PieChart
          data={showData}
          hideFilter
          detailPopoverContent={(datum: any, sum) => [
            { key: '数据列名', value: datum.name },
            { key: '数量', value: datum.value?.toLocaleString() },
            {
              key: '占比',
              value: `${((datum.value / sum) * 100).toFixed(0)}%`,
            },
          ]}
          segmentDescription={(datum, sum) =>
            `${datum.value?.toLocaleString()} , ${(
              ((datum.value || 0) / sum) *
              100
            ).toFixed(0)}%`
          }
          ariaLabel={oldMessage.content}
          empty={
            <Box textAlign="center" color="inherit">
              <b>No data available</b>
              <Box variant="p" color="inherit">
                There is no data available
              </Box>
            </Box>
          }
          noMatch={
            <Box textAlign="center" color="inherit">
              <b>No matching data</b>
              <Box variant="p" color="inherit">
                There is no matching data to display
              </Box>
            </Box>
          }
        />
      </div>
    ) : (
      <></>
    );
  } catch {
    return <></>;
  }
});
ChartPie.displayName = 'ChartPie';
export default ChartPie;
