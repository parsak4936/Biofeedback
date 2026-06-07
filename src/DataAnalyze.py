import pandas as pd
import matplotlib.pyplot as plt
import sys

if len(sys.argv) != 2:
    print("usage: DataAnalyze <data_file>")
    sys.exit(1)
else:
    file_path = sys.argv[1]

# Define the path to your CSV file
#file_path = 'P001_easy_relax_20260420_141714.csv'

try:
    # Read the CSV file into a DataFrame
    df = pd.read_csv(file_path, comment='#')
    
    # Print the column names (headers)
    #print(f"Headers: {list(df.columns)}")
    
    # Print the first 5 rows
    #print(df.head())
    
except FileNotFoundError:
    print(f"The file {file_path} does not exist.")
    sys.exit(1)

baseline_df = df.query("eventType =='data' and baselineStatus == 'collecting'")
data_df = df.query("eventType =='data' and baselineStatus == 'complete' and balloonState != 'waiting'")
#print(baseline_df.head())

balloonHeight = data_df['balloonHeight']
#print(balloonHeight)
avgBalloon = balloonHeight.mean()
stdBalloon = balloonHeight.std()
print(f"AVG Balloon Height: {avgBalloon:.2f}, STD Balloon Height: {stdBalloon:.2f}")

columns_to_extract = ['rawEDA', 'rawHRV', 'rawHR']
raw_df = baseline_df[columns_to_extract]
#print(raw_df.head())

#stats = pd.concat([raw_df.mean(), raw_df.std()], axis=1)
#print(stats)

avgs = raw_df.mean()
stds = raw_df.std()
avgEDA = avgs['rawEDA']
avgHRV = avgs['rawHRV']
avgHR = avgs['rawHR']
print(f"AVG EDA: {avgEDA:.2f}, AVG HRV: {avgHRV:.2f}, AVG HR: {avgHR:.2f}")

normEDA = (data_df['rawEDA'] - avgEDA) / avgEDA * 100
normHRV = (avgHRV - data_df['rawHRV']) / avgHRV * 100
normHR = (data_df['rawHR'] - avgHR) / avgHR * 100

#print(normEDA, normHRV, normHR)

stressIndex = (.5 * normEDA + .3 * normHRV + .2 * normHR).rolling(window=50).mean()
stdStress = stressIndex.std()
print(f"STD Stress: {stdStress:.2f}")

correlation = stressIndex.corr(balloonHeight)
print(f"Height-Stress Correlation: {correlation:.2f}")

#print(stressIndex)
fig, axs = plt.subplots(2,figsize=(10, 12))
axs[0].plot(data_df['timestamp'], stressIndex, label='Stress Index', color='blue', linestyle='solid')
axs[0].set_title('Stress Index vs Time', fontsize=14)
axs[0].set_xlabel('Time (s)')
axs[0].set_ylabel('Stress Index (%)')
axs[0].axhline(y=stdStress*1.33, color='orange', linestyle='-', linewidth=1, alpha=0.5, label='1.33 * STD')
axs[0].axhline(y=stdStress*2.28, color='red', linestyle='-', linewidth=1.5, alpha=0.5, label='2.28 * STD')
axs[0].grid(True, axis='y', linestyle='--', alpha=0.3)
axs[0].legend(loc='lower right', fontsize=10)

axs[1].plot(data_df['timestamp'], balloonHeight, label='Balloon Height', color='blue', linestyle='solid')
axs[1].set_title('Balloon Height vs Time', fontsize=14)
axs[1].set_xlabel('Time (s)')
axs[1].set_ylabel('Height (m)')
axs[1].grid(True, axis='y', linestyle='--', alpha=0.3)

plt.savefig(file_path + '.png')
plt.show()