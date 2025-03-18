import { memo, useContext, useEffect, useLayoutEffect, useState } from 'react';

import { Message } from '@/types/chat';

import HomeContext from '@/pages/api/home/home.context';

import BarChart from '@cloudscape-design/components/bar-chart';
import Box from '@cloudscape-design/components/box';

const ChartBar = memo((props: { message: Message; oldMessage: Message }) => {
  const {
    state: { lightMode },
  } = useContext(HomeContext);
  const [showData, setShowData] = useState([] as any);
  const [xDomain, setXDomain] = useState([] as any);
  const [yDomain, setYDomain] = useState([] as any);
  const { message, oldMessage } = props;
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
    if (chartDataObj.entity_name) {
      const valueKeyList = Object.keys(chartDataObj.entity_name);
      if (chartDataObj.index_value) {
        valueKeyList.forEach((item, index) => {
          tempShowData.push({
            x: chartDataObj.entity_name[item],
            y: chartDataObj.index_value[item],
          });
        });
      } else {
        const valuesObj = getDefaultValue(chartDataObj);
        valueKeyList.forEach((item, index) => {
          tempShowData.push({
            x: chartDataObj.entity_name[item],
            y: valuesObj[item],
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
      valueKeyList.forEach((item) => {
        tempShowData.push({
          x: keyValueIndex[item],
          y: valuesObj[item],
        });
      });
    }
    setShowData(tempShowData);
    setXDomain(tempShowData.map((i: { x: any }) => i.x));
    setYDomain([
      0,
      Math.max(...tempShowData.map((i: { y: any }) => i.y)) || 9999,
    ]);
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
  if (!message || !message.chartData) {
    return <></>;
  }
  if (!chartDataObj) {
    return <></>;
  }

  return showData && showData.length > 0 ? (
    <div className={lightMode === 'dark' ? 'dark-mode-chart' : ''}>
      <BarChart
        series={[
          {
            title: chartDataObj.index_name
              ? chartDataObj.index_name['0']
              : oldMessage.content,
            type: 'bar',
            data: showData,
            valueFormatter: (e: any) => e?.toLocaleString(),
          },
        ]}
        xDomain={xDomain}
        yDomain={yDomain}
        i18nStrings={{
          xTickFormatter: (e) => {
            return e.toString();
          },
          yTickFormatter: function o(e) {
            return e.toLocaleString();
          },
        }}
        ariaLabel="数据图表"
        height={300}
        xTitle={chartDataObj.index_type ? chartDataObj.index_type['0'] : '数据'}
        yTitle={chartDataObj.index_name ? chartDataObj.index_name['0'] : '数据'}
        hideFilter
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
});
ChartBar.displayName = 'ChartBar';
export default ChartBar;
